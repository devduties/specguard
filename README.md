# SpecGuard

SpecGuard is an OpenAPI request-validation proxy. It checks incoming requests against an API contract and forwards valid requests to the configured upstream service.

The project is being built in small, reviewable steps. The first slice matches an incoming path and method to an OpenAPI operation, including path parameters and literal-route precedence. Request validation and proxy forwarding are planned as later slices.

## Development

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
ruff check .
pytest -q
```
