# SpecGuard

SpecGuard sits in front of your API and checks every request against an OpenAPI contract before it reaches your service. Valid requests go through; invalid ones get a clear error response and never touch the upstream.

Run the [local orders demo](examples/demo/README.md) to see it working in about two minutes.

## Run locally

You need a local OpenAPI contract file and a running upstream service. This example assumes your contract is `openapi.yaml` and the upstream is on port 9000.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
specguard --spec .\openapi.yaml --upstream http://127.0.0.1:9000 --port 8000
```

`python -m specguard` works the same way. Run `--help` to see all options.

SpecGuard validates the contract and settings before starting the listener, so a bad contract or missing flag gives you a clear error at startup, not at runtime. Press Ctrl+C to stop. To pick up a contract change, restart the process.

If your upstream URL has a base path like `/api`, SpecGuard prepends it automatically. Declare your paths in the contract without it.

| Flag | Environment variable | Default |
| --- | --- | --- |
| `--spec` | `SPECGUARD_SPEC` | Required |
| `--upstream` | `SPECGUARD_UPSTREAM` | Required |
| `--host` | `SPECGUARD_HOST` | `127.0.0.1` |
| `--port` | `SPECGUARD_PORT` | `8000` |
| `--timeout` | `SPECGUARD_TIMEOUT` | `10` seconds |
| `--max-request-bytes` | `SPECGUARD_MAX_REQUEST_BYTES` | `1048576` |
| `--max-response-bytes` | `SPECGUARD_MAX_RESPONSE_BYTES` | `8388608` |

Flags take priority over environment variables. You can also configure everything through the environment without any flags:

```powershell
$env:SPECGUARD_SPEC = ".\openapi.yaml"
$env:SPECGUARD_UPSTREAM = "http://127.0.0.1:9000"
$env:SPECGUARD_PORT = "8000"
python -m specguard
```

These are process environment variables. `.env` files are not loaded automatically.

The server uses Starlette directly rather than FastAPI. Routing and validation come from the OpenAPI contract, so FastAPI's endpoint declarations and schema generation would just be in the way.

## Request logs

Every request gets a UUID4 hex ID. It goes back to the client as `X-Request-ID`, gets forwarded to upstream under the same header, and appears in the log record. Any ID the client sends is replaced — this keeps the log and the response in sync.

SpecGuard writes one JSON record per request to stderr:

```json
{"timestamp":"2026-11-03T18:00:00+00:00","level":"info","event":"request_finished","request_id":"23d9989203144b39911044b16363b66b","method":"post","route":"/orders","status_code":201,"duration_ms":25.0,"outcome":"upstream_response","error_code":null}
```

`method` and `route` come from the matched operation — `route` is the path template like `/orders/{order_id}`, not the actual path. Both are null if no operation matched. Paths, query strings, headers, bodies, and error messages are never logged.

`duration_ms` covers the `forward()` call: parameter and body validation plus the upstream round-trip. It doesn't include reading the incoming body or sending the response back. It's null if the request stops before that point (oversized body, disconnected client).

Log levels: INFO for success, WARNING for 4xx and client disconnects, ERROR for 5xx, cancellation, and send failures.

`outcome` is `upstream_response` for anything that reached upstream (including upstream 4xx/5xx), `proxy_error` for requests blocked before forwarding, `client_disconnected`, `cancelled`, or `response_failed` for interrupted requests.

If you're using `create_app()` directly, request records go through the `specguard.requests` logger using `specguard.request_logging.RequestFormatter`. Wire it up however fits your setup.

## How validation works

SpecGuard validates three things in order before forwarding: the route, the parameters, then the request body. If any step fails the request is rejected immediately and never reaches upstream.

### Contract files

SpecGuard reads local JSON or YAML files and supports OpenAPI 3.0, 3.1, and 3.2. Internal `$ref` references work. References to other files or URLs are not supported.

```python
from specguard.spec_loader import load_openapi_spec
from specguard.routing import resolve_operation

spec = load_openapi_spec("openapi.yaml")
match = resolve_operation(spec, "/orders/order-42?include=items", "GET")
print(match.path_template, match.path_parameters)
```

An unmatched path raises `RouteNotFoundError`. A matched path with a wrong method raises `MethodNotAllowedError`. Both carry an HTTP status code.

### Parameters

`validate_parameters` checks path, query, header, and cookie parameters against the contract. It returns typed values — integers, booleans, arrays, objects — without modifying the input or injecting defaults.

```python
from specguard.parameters import validate_parameters

parameters = validate_parameters(
    spec, "/orders/42?limit=10", "GET", headers={"X-Client": "demo"}
)
print(parameters.path, parameters.query, parameters.headers, parameters.cookies)
```

Supported parameter styles:

- Path: `simple`, `label`, `matrix` (both explode settings)
- Query: `form`, `spaceDelimited`, `pipeDelimited`, `deepObject`
- Header: `simple`
- Cookie: `form`

Type coercion follows the schema. Strings stay strings when the schema allows them. `oneOf` rejects a value that matches more than one branch. Encoded delimiters stay as data. Repeated scalar parameters are rejected. Undeclared parameters are ignored unless the schema is free-form.

`ParameterValidationError` (400) lists every issue found, not just the first. An invalid style combination raises `ParameterDefinitionError` — that's a contract problem, not a bad request.

### Request body

`validate_request_body` checks the request body against the contract's schema. It returns the parsed value. The forwarding layer sends the original bytes, not the parsed result.

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

Required bodies reject missing or empty input. Optional bodies can be absent. JSON `null` counts as present and must satisfy the schema. A body sent to an operation that doesn't declare one is rejected.

Only JSON content types are supported (`application/json` and `+json` variants). The Content-Type match is case-insensitive and ignores `charset` parameters. An exact match wins over `type/*`, which wins over `*/*`.

`BodyValidationError` (400) includes a path for each issue — field names and array indexes relative to the JSON root. `UnsupportedMediaTypeError` is 415.

## Error responses

All validation errors use the same envelope:

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

| Exception | Status | Code |
| --- | --- | --- |
| `ParameterValidationError` | 400 | `parameter_validation_failed` |
| `BodyValidationError` | 400 | `body_validation_failed` |
| `UnsupportedMediaTypeError` | 415 | `unsupported_media_type` |
| `RouteNotFoundError` | 404 | `route_not_found` |
| `MethodNotAllowedError` | 405 | `method_not_allowed` |
| `RequestTooLargeError` | 413 | `request_too_large` |
| `UpstreamError` | 502 | `upstream_error` |
| `UpstreamTimeoutError` | 504 | `upstream_timeout` |
| Contract or unexpected errors | 500 | `internal_error` |

Error messages come from a fixed list. Raw schema messages, submitted values, and stack traces are never included in client responses. The 500 envelope is always generic.

## Forwarding

`UpstreamProxy` owns one async HTTP client shared across requests. Each `forward()` call validates the request first, then sends it.

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

Request targets must be origin-form (starting with `/`). Encoded values, double slashes, and duplicate query parameters are preserved. Dot segments, fragments, backslashes, and control characters are rejected.

Hop-by-hop headers are stripped both ways, including anything nominated by a `Connection` header. The outbound `Host` and `Content-Length` are set by the proxy. Upstream cookies are not stored or replayed between requests. Redirects and retries are disabled.

Compressed responses come back as-is — SpecGuard doesn't decompress or re-encode them.

The defaults are 10s timeout, 1 MiB request cap, 8 MiB response cap.

## Development

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
ruff check .
pytest -q
```
