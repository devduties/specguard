"""Validate requests and forward their original bytes to a configured upstream."""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx

from specguard.errors import RequestValidationError, UpstreamError
from specguard.parameters import validate_parameters
from specguard.request_body import validate_request_body

_HOP_HEADERS = {
    b"connection",
    b"keep-alive",
    b"proxy-authenticate",
    b"proxy-authorization",
    b"proxy-connection",
    b"te",
    b"trailer",
    b"transfer-encoding",
    b"upgrade",
}
_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9a-zA-Z-]+")


class ProxyRequestError(RequestValidationError):
    code = "invalid_proxy_request"
    public_message = "The request cannot be forwarded."

    def __init__(self, message: str) -> None:
        super().__init__(message, ())


class RequestTooLargeError(ProxyRequestError):
    status_code = 413
    code = "request_too_large"
    public_message = "Request body exceeds the configured size limit."


class UnsupportedContentEncodingError(ProxyRequestError):
    status_code = 415
    code = "unsupported_content_encoding"
    public_message = "Encoded request bodies are not supported."


class UpstreamTimeoutError(UpstreamError):
    status_code = 504
    code = "upstream_timeout"
    public_message = "The upstream service timed out."


class UpstreamResponseTooLargeError(UpstreamError):
    code = "upstream_response_too_large"
    public_message = "The upstream response exceeds the configured size limit."


@dataclass(frozen=True, slots=True)
class ProxySettings:
    upstream_url: str
    timeout_seconds: float = 10.0
    max_request_bytes: int = 1024 * 1024
    max_response_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        try:
            parsed = urlsplit(self.upstream_url)
            port = parsed.port
            httpx.URL(self.upstream_url)
        except (ValueError, httpx.InvalidURL) as exc:
            raise ValueError("Invalid upstream URL.") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or port == 0
            or "?" in self.upstream_url
            or "#" in self.upstream_url
        ):
            raise ValueError(
                "Upstream must be an HTTP(S) URL without credentials, query, or fragment."
            )
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive.")
        for name in ("max_request_bytes", "max_response_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer.")


@dataclass(frozen=True, slots=True)
class UpstreamResponse:
    status_code: int
    body: bytes
    headers: tuple[tuple[bytes, bytes], ...]


class UpstreamProxy:
    """Own one connection pool; each call validates before making one upstream request."""

    def __init__(
        self,
        document: dict[str, Any],
        settings: ProxySettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        content_decoders: Mapping[str, Callable[[str], Any]] | None = None,
    ) -> None:
        self.document = document
        self.settings = settings
        self.transport = transport
        self.content_decoders = content_decoders
        self._base_url = httpx.URL(settings.upstream_url)
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> UpstreamProxy:
        if self._client is not None:
            raise RuntimeError("UpstreamProxy is already open.")
        self._client = httpx.AsyncClient(
            timeout=self.settings.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            transport=self.transport,
            cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
        )
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def forward(
        self,
        request_target: str,
        request_method: str,
        headers: Mapping[str, str] | Sequence[tuple[str, str]] = (),
        body: bytes = b"",
    ) -> UpstreamResponse:
        if self._client is None:
            raise RuntimeError("Use UpstreamProxy as an async context manager.")
        if len(body) > self.settings.max_request_bytes:
            raise RequestTooLargeError("Request body exceeds the configured size limit.")
        if not _TOKEN.fullmatch(request_method):
            raise ProxyRequestError("Invalid request method.")
        target = _origin_target(request_target)
        prefix = self._base_url.raw_path.rstrip(b"/")
        url = self._base_url.copy_with(raw_path=prefix + target.encode("ascii"))
        try:
            raw_headers = httpx.Headers(headers).raw
            for name, value in raw_headers:
                if not _TOKEN.fullmatch(name.decode("ascii")) or any(
                    c in value for c in (b"\r", b"\n", b"\0")
                ):
                    raise ProxyRequestError("Invalid request header.")
            request = httpx.Request(
                request_method,
                url,
                headers=_forward_headers(raw_headers, request=True),
                content=body,
            )
        except (ValueError, UnicodeError) as exc:
            raise ProxyRequestError("Invalid request headers.") from exc
        # HTTPX uppercases methods on construction, but custom methods are case-sensitive.
        request.method = request_method
        content_types = request.headers.get_list("content-type")
        if len(content_types) > 1:
            raise ProxyRequestError("Content-Type must occur only once.")
        if request.headers.get("content-encoding", "identity").strip().lower() != "identity":
            raise UnsupportedContentEncodingError("Encoded request bodies are not supported.")
        validate_parameters(
            self.document,
            target,
            request_method,
            request.headers.multi_items(),
            content_decoders=self.content_decoders,
        )
        validate_request_body(
            self.document,
            target,
            request_method,
            body,
            content_type=content_types[0] if content_types else None,
        )

        try:
            async with asyncio.timeout(self.settings.timeout_seconds):
                response = await self._client.send(request, stream=True, follow_redirects=False)
                try:
                    chunks = []
                    size = 0
                    async for chunk in response.aiter_raw():
                        size += len(chunk)
                        if size > self.settings.max_response_bytes:
                            raise UpstreamResponseTooLargeError(
                                "Upstream response size limit exceeded."
                            )
                        chunks.append(chunk)
                    return UpstreamResponse(
                        response.status_code,
                        b"".join(chunks),
                        tuple(_forward_headers(response.headers.raw)),
                    )
                finally:
                    await response.aclose()
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise UpstreamTimeoutError("Upstream request timed out.") from exc
        except httpx.LocalProtocolError as exc:
            raise ProxyRequestError("Invalid outbound request.") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError("Upstream request failed.") from exc


def _forward_headers(
    headers: list[tuple[bytes, bytes]], *, request: bool = False
) -> list[tuple[bytes, bytes]]:
    blocked = set(_HOP_HEADERS)
    for name, value in headers:
        if name.lower() == b"connection":
            blocked.update(token.strip().lower() for token in value.split(b","))
    if request:
        blocked.update({b"host", b"content-length"})
    return [(name, value) for name, value in headers if name.lower() not in blocked]


def _origin_target(target: str) -> str:
    if (
        not target.startswith("/")
        or "#" in target
        or "\\" in target
        or any(ord(character) < 32 or ord(character) == 127 for character in target)
    ):
        raise ProxyRequestError(
            "Expected an origin-form request target without control characters."
        )
    path, separator, query = target.partition("?")
    if any(unquote(segment) in {".", ".."} for segment in path.split("/")):
        raise ProxyRequestError("Dot segments are not supported in forwarded paths.")
    safe = "/%:@!$&'()*+,;=-._~"
    return quote(path, safe=safe) + ("?" + quote(query, safe=safe + "?=") if separator else "")
