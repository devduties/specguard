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

## Development

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
ruff check .
pytest -q
```
