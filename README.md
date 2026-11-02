# SpecGuard

SpecGuard is an OpenAPI request-validation proxy that checks incoming requests against an API contract and forwards valid requests to the configured upstream service.

Run the [local orders demo](examples/demo/README.md) to see valid requests forwarded and invalid requests blocked.

## Supported contract files

SpecGuard reads local JSON and YAML files (`.json`, `.yaml`, or `.yml`) and validates OpenAPI 3.0, 3.1, and 3.2 documents when they are loaded. Internal `$ref` references are supported. References to other files or URLs are not supported in this first slice.

```python
from specguard.spec_loader import load_openapi_spec
from specguard.routing import resolve_operation

spec = load_openapi_spec("openapi.yaml")
match = resolve_operation(spec, "/orders/order-42?include=items", "GET")
print(match.path_template, match.path_parameters)
```

`resolve_operation` raises `RouteNotFoundError` for an unmatched path and `MethodNotAllowedError` when the path exists but does not define the requested method. Both include an HTTP status code. Run SpecGuard with the CLI below, or use `UpstreamProxy` directly as a library.

## Run locally

Provide a local OpenAPI contract and a running upstream service. The example
below assumes your contract is `openapi.yaml` and the upstream listens on port
9000. For a complete runnable example, follow the [local orders demo](examples/demo/README.md).

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
specguard --spec .\openapi.yaml --upstream http://127.0.0.1:9000 --port 8000
```

`python -m specguard` accepts the same arguments as `specguard`. Use `--help` to
see all options. Send requests to `http://127.0.0.1:8000` using the paths declared
in your contract. If the upstream base URL includes a prefix such as `/api`,
declare the paths without that prefix; the forwarding layer prepends it.

The contract and configuration are checked before the listener starts. Missing
or invalid contracts and settings produce an actionable startup error and a
nonzero exit code. Press Ctrl+C to stop; shutdown closes the shared upstream
client. Restart the process after changing the contract. Hot reload is not
enabled in this milestone.

| CLI flag | Environment variable | Default |
| --- | --- | --- |
| `--spec` | `SPECGUARD_SPEC` | Required |
| `--upstream` | `SPECGUARD_UPSTREAM` | Required |
| `--host` | `SPECGUARD_HOST` | `127.0.0.1` |
| `--port` | `SPECGUARD_PORT` | `8000` |
| `--timeout` | `SPECGUARD_TIMEOUT` | `10` seconds |
| `--max-request-bytes` | `SPECGUARD_MAX_REQUEST_BYTES` | `1048576` |
| `--max-response-bytes` | `SPECGUARD_MAX_RESPONSE_BYTES` | `8388608` |

Flags override environment variables. Environment-only startup works too:

```powershell
$env:SPECGUARD_SPEC = ".\openapi.yaml"
$env:SPECGUARD_UPSTREAM = "http://127.0.0.1:9000"
$env:SPECGUARD_PORT = "8000"
python -m specguard
```

These are process environment variables; `.env` files are not loaded automatically.
The server runs as one process, binds to loopback by default, and accepts HTTP
requests for contract routes. Uvicorn access logs are disabled; SpecGuard emits
the structured request records described below. HTTP-to-WebSocket upgrades are not enabled, and the server
does not add a dashboard, documentation routes, or health endpoints.

The proxy adapter uses Starlette directly because routing and validation come from the supplied OpenAPI contract, so FastAPI's endpoint declarations and schema generation would duplicate that work.

## Request logs and IDs

The CLI writes one JSON record per HTTP request to stderr. Each request gets a
new UUID4 hex ID, returned as `X-Request-ID` and sent to upstream under the same
header. Client-supplied IDs, including duplicates, are replaced; upstream response
IDs are also replaced so the response always agrees with the request log.
The generated header is included in upstream request-header validation.

```json
{"timestamp":"2026-11-03T18:00:00+00:00","level":"info","event":"request_finished","request_id":"23d9989203144b39911044b16363b66b","method":"post","route":"/orders","status_code":201,"duration_ms":25.0,"outcome":"upstream_response","error_code":null}
```

`method` is the matched contract method and `route` is its path template, such
as `/orders/{order_id}`. Both are null when no operation was matched. Logs omit
raw paths, queries, headers, bodies, and exception messages. `duration_ms` measures
the call to `proxy.forward()`, including validation and the upstream exchange;
it excludes reading the incoming body and sending the response. It is null if
the request stops before that call, such as an oversized upload or disconnect.

Successful responses use INFO, 4xx responses and client disconnects use WARNING,
and 5xx responses, cancellation, and response-send failures use ERROR. `outcome`
distinguishes `upstream_response` (including upstream 4xx/5xx) from `proxy_error`;
proxy errors also include the stable envelope `error_code`. Interrupted requests
use `client_disconnected`, `cancelled`, or `response_failed`; `status_code` is
null until a response is prepared and does not guarantee delivery to the client.

Uvicorn lifecycle messages retain their standard format. Applications embedding
`create_app()` control their own logging configuration; request records use the
`specguard.requests` logger and `specguard.request_logging.RequestFormatter`.

## Request parameter validation

`validate_parameters` routes a request, combines Path Item and operation parameters
(operation definitions override the same name and location), and returns typed
path, query, header, and cookie values. Header lookup is case-insensitive. Missing optional
parameters are omitted; defaults are not injected and inputs are not modified.

```python
from specguard.parameters import validate_parameters

parameters = validate_parameters(
    spec, "/orders/42?limit=10", "GET", headers={"X-Client": "demo"}
)
print(parameters.path, parameters.query, parameters.headers, parameters.cookies)
```

Parameters support primitive types, arrays, objects, nullable values, type lists,
and `allOf`/`anyOf`/`oneOf`. Required values, schema constraints, and known formats
are checked using the contract's OpenAPI version. Local JSON Pointer parameter
and schema references are supported. Strings are preserved when a union permits
them; otherwise possible conversions are checked against the complete schema.
For example, `oneOf` still rejects a converted value matching multiple branches.

Supported styles:

- Path: `simple`, `label`, and `matrix`, with either explode setting.
- Query: `form`, `spaceDelimited`, `pipeDelimited`, and `deepObject`.
- Header: `simple`.
- Cookie: `form`, using semicolon-separated cookie pairs. Exploded arrays use
  repeated cookie names; exploded objects use property names as cookie names.

Encoded commas, dots, and semicolons remain data when splitting structural
delimiters. Space/pipe query delimiters accept their percent-encoded forms; data
that collides with those delimiters must be pre-encoded as described by OpenAPI.
Cookie values use percent-decoding without replacing plus signs with spaces;
header values are not percent-decoded. Repeated scalar parameters and repeated
object properties are rejected. Undeclared parameters are ignored, except fields
belonging to free-form exploded objects. Other declared parameters' names and
object properties are excluded from free-form object collection.
Repeated array or object header lines are combined in their original order;
scalar header parameters still reject duplicate lines. Referenced and composed
closed objects ignore unrelated query fields in the same way as inline objects.

Content-encoded parameters support JSON (including `+json` media types) and
`text/plain`. JSON supports nested arrays and objects without coercing JSON
strings into numbers. Other media types can supply a callable through the
`content_decoders` mapping; decoded values still undergo schema validation.
JSON numeric overflow is rejected, including inside nested objects and arrays.

`ParameterValidationError` carries a tuple of location/name/message issues and a
400 status code. Routing errors retain their existing behavior. Invalid or
undefined style combinations raise `ParameterDefinitionError`, including
`deepObject` without `explode=true` and exploded space/pipe styles. OpenAPI does
not define nested `deepObject` property serialization; use JSON content for nested
objects or arrays. Conversion is bounded to 32 nesting levels and 128 typed
interpretations per parameter. OpenAPI's reserved Accept, Content-Type, and
Authorization header parameter definitions are ignored.

## JSON request body validation

`validate_request_body` routes a request, checks the operation's request body,
and returns the parsed JSON value. The caller supplies raw UTF-8 bytes (or text)
and the Content-Type separately.

```python
from specguard.request_body import validate_request_body

body = validate_request_body(
    spec,
    "/orders",
    "POST",
    b'{"name":"coffee","quantity":2}',
    content_type="application/json",
)
print(body.present, body.value)
```

Required bodies reject missing or zero-length input. Optional bodies can be
absent; whitespace is a present, invalid JSON body. JSON `null` is also present
and must satisfy the schema. Bodies supplied to operations without a request-body
declaration are rejected. Validation does not coerce values, insert defaults,
change the contract, or modify the original body.

Supported media types are JSON and declared `+json` types. Content-Type matching
is case-insensitive and ignores parameters such as `charset=utf-8`; bytes must
always be UTF-8. An exact content entry wins over `type/*`, which wins over `*/*`.
An `application/json` declaration does not implicitly permit every `+json` type.
Non-JSON bodies, form data, multipart uploads, and streaming JSON are not
supported in this milestone.

Schema validation uses the contract's OpenAPI 3.0, 3.1, or 3.2 rules and known
format checks. It covers nested objects and arrays, required properties, types,
enums, bounds, string constraints, and composed schemas. Internal request-body,
schema, and OpenAPI 3.2 media-type references are supported; external references
remain unsupported. OpenAPI 3.0 bodies use request-specific readOnly/writeOnly
rules. In 3.1 and 3.2 those keywords retain the library's annotation semantics.

`BodyValidationError` has status code 400 and an `issues` tuple of `BodyIssue`
records. Each issue contains a `path` tuple of field names and array indexes,
plus a message. Malformed JSON, invalid UTF-8, and nonfinite numbers (including
overflow inside nested values) are rejected. `UnsupportedMediaTypeError` is a
body-validation error with status code 415. Unresolved contract references raise
`BodyDefinitionError`; routing errors retain their existing behavior. The
contract should be loaded and validated before validating requests.

The parsed value is for validation and inspection. The forwarding layer sends
the original body bytes without reserializing the parsed JSON.

## Consistent validation errors

`error_response` converts parameter and body validation exceptions into a common,
JSON-serializable error envelope. The response's `status_code` is separate from
its JSON payload so an HTTP adapter can set the response status directly.

```python
from specguard.errors import error_response
from specguard.parameters import ParameterValidationError, validate_parameters
from specguard.request_body import BodyValidationError, validate_request_body

try:
    validate_parameters(spec, target, method, headers)
    validate_request_body(spec, target, method, raw_body, content_type=content_type)
except (ParameterValidationError, BodyValidationError) as exc:
    response = error_response(exc)
    status = response.status_code
    payload = response.to_dict()
```

A missing required query parameter produces status 400 and this payload:

```json
{
  "error": {
    "code": "parameter_validation_failed",
    "message": "Request parameters do not satisfy the contract.",
    "details": [
      {
        "location": "query",
        "name": "limit",
        "path": [],
        "code": "required",
        "message": "Required value is missing."
      }
    ]
  }
}
```

Every envelope has `error.code`, `error.message`, and an `error.details` array.
Every detail has `location`, `name`, `path`, `code`, and `message`. Parameter
details identify the location and parameter name, with an empty path. Body
details use location `body`, name `null`, and a path of field names and array
indexes relative to the JSON root. Missing-property errors identify their
containing object, as reported by the schema validator.

| Exception | HTTP status | Error code |
| --- | --- | --- |
| `ParameterValidationError` | 400 | `parameter_validation_failed` |
| `BodyValidationError` | 400 | `body_validation_failed` |
| `UnsupportedMediaTypeError` | 415 | `unsupported_media_type` |
| Contract or unexpected exceptions | 500 | `internal_error` |

Client-facing messages come from a fixed set, including codes for missing values,
types, enums, formats, common constraints, JSON syntax, and text encoding. Other
detail codes fall back to `invalid_value`. Raw schema messages, submitted values,
decoder exceptions, and stack traces are not included in response messages.
Existing exception `issues` and diagnostic strings remain available to callers;
serialize the envelope instead of sending `str(exc)` to clients.

Contract and unexpected exceptions passed to `error_response` produce a generic
500 envelope with an empty details array. Handle and log the original exception
on the server as appropriate. Routing errors retain their exception types; the
HTTP adapter maps them to the same envelope using `route_not_found` (404),
`method_not_allowed` (405), and `invalid_request_target` (400). A 405 response
includes the Allow header. Unexpected server failures return a generic 500
envelope without exception text or stack traces.

## Forwarding validated requests

`UpstreamProxy` owns a reusable async HTTP client. Each `forward` call validates
the operation, parameters, and JSON body before sending anything upstream.
Load and validate the contract with `load_openapi_spec` before constructing the
proxy. The upstream base URL is configured by the operator, not by the caller.

```python
import asyncio

from specguard.forwarding import ProxySettings, UpstreamProxy
from specguard.spec_loader import load_openapi_spec

async def main():
    spec = load_openapi_spec("openapi.yaml")
    settings = ProxySettings(
        upstream_url="http://127.0.0.1:9000/api",
        timeout_seconds=10,
        max_request_bytes=1024 * 1024,
        max_response_bytes=8 * 1024 * 1024,
    )
    async with UpstreamProxy(spec, settings) as proxy:
        response = await proxy.forward(
            "/orders?notify=true",
            "POST",
            headers={"Content-Type": "application/json"},
            body=b'{ "name" : "coffee", "quantity" : 2 }',
        )
        print(response.status_code, response.body)

asyncio.run(main())
```

Pass origin-form request targets beginning with `/`. A configured base path is
prepended, so `/orders` above reaches `/api/orders`. Leading double slashes,
percent-encoded values, duplicate query parameters, and original body bytes are
preserved. Unicode targets are percent-encoded before validation and forwarding.
Absolute request URLs, fragments, backslashes, control characters, and dot path
segments are rejected rather than reinterpreted. The method's capitalization is
preserved, including custom methods. Request bodies must be bytes.

Request and response hop-by-hop headers are removed, including fields nominated
by any `Connection` header. The outbound Host is set for the configured upstream,
and Content-Length is recalculated from the buffered body. Validation sees those
outbound headers, so a Connection-nominated field cannot pass validation and then
disappear upstream. Ordinary headers such as Authorization and Cookie are passed
through. Duplicate Content-Type fields and non-identity request Content-Encoding
are rejected. Response headers are retained as a tuple of byte pairs, preserving
duplicates such as Set-Cookie. Compressed response bytes and their Content-Encoding
remain together; the proxy does not decompress or reserialize the response.

Redirects and automatic retries are disabled. The client does not retain or
replay upstream cookies between callers, use environment proxy settings, or
disable TLS verification. Optional parameter media-type decoders can be supplied
through `UpstreamProxy(..., content_decoders=...)`, just as with `validate_parameters`.

Defaults are a 10-second timeout, a 1 MiB request-body cap, and an 8 MiB response-body
cap. The timeout bounds the upstream exchange, including reading the response;
HTTPX also applies connect, read, write, and pool timeouts. Oversized requests are
blocked before validation or transmission. Responses are read as raw chunks up to
the configured cap and the stream is closed on completion or failure. Upstream
4xx/5xx statuses and redirect responses are returned unchanged, rather than
treated as transport failures.

| Exception | Status | Envelope code |
| --- | --- | --- |
| `ProxyRequestError` | 400 | `invalid_proxy_request` |
| `RequestTooLargeError` | 413 | `request_too_large` |
| `UnsupportedContentEncodingError` | 415 | `unsupported_content_encoding` |
| `UpstreamError` | 502 | `upstream_error` |
| `UpstreamResponseTooLargeError` | 502 | `upstream_response_too_large` |
| `UpstreamTimeoutError` | 504 | `upstream_timeout` |

These exceptions work with `error_response`; upstream failure envelopes omit
private connection details. Existing parameter/body validation errors and routing
exceptions still propagate without an upstream call. The HTTP adapter sets the
response status, preserves response header pairs, and writes the returned body
bytes. It enforces the request-size cap while receiving chunks, even without
Content-Length. Disconnected clients are not forwarded. This implementation
buffers bodies; it does not provide streaming uploads, WebSockets, or
response-schema validation.

## Development

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
ruff check .
pytest -q
```
