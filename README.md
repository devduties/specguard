# SpecGuard

SpecGuard is an OpenAPI request-validation proxy. It checks incoming requests against an API contract and forwards valid requests to the configured upstream service.

The core is being built in small, reviewable steps. SpecGuard can load and validate a local OpenAPI contract, then match a request target and method to an operation. It extracts path parameters, ignores the query string during route matching, decodes percent-encoded path segments, and gives literal routes precedence over parameterized routes.

## Supported contract files

SpecGuard reads local JSON and YAML files (`.json`, `.yaml`, or `.yml`) and validates OpenAPI 3.0, 3.1, and 3.2 documents when they are loaded. Internal `$ref` references are supported. References to other files or URLs are not supported in this first slice.

```python
from specguard.spec_loader import load_openapi_spec
from specguard.routing import resolve_operation

spec = load_openapi_spec("openapi.yaml")
match = resolve_operation(spec, "/orders/order-42?include=items", "GET")
print(match.path_template, match.path_parameters)
```

`resolve_operation` raises `RouteNotFoundError` for an unmatched path and `MethodNotAllowedError` when the path exists but does not define the requested method. Both include an HTTP status code for the future proxy layer to use. The current project does not yet run an HTTP server or forward requests; those are later roadmap steps.

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

Proxy forwarding and configurable request-size limits remain later roadmap
steps. The parsed value is for validation and
inspection; forward the original body bytes when the proxy layer is added.

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
on the server as appropriate. Routing errors retain their current API and are
outside this increment's adapter; map them separately when adding the HTTP layer.
This feature does not start a server or forward requests.

## Development

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
ruff check .
pytest -q
```
