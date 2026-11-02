# SpecGuard local demo

This walkthrough runs the SpecGuard proxy in front of a small orders API and
shows a valid request reaching upstream while an invalid request is blocked.
Run all commands from the repository root. Three terminals are used: upstream,
proxy, and verification. No Docker or external service is required.

## Install once

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

If the development environment is already installed, reuse it.

## Start the upstream in terminal one

```powershell
.\.venv\Scripts\python.exe -m uvicorn examples.demo.upstream:app --host 127.0.0.1 --port 9000 --no-access-log
```

The upstream stores orders in memory. It deliberately leaves contract validation
to SpecGuard. Its data and request counter reset when this process restarts.

## Start SpecGuard in terminal two

```powershell
.\.venv\Scripts\python.exe -m specguard --spec .\examples\demo\openapi.yaml --upstream http://127.0.0.1:9000 --host 127.0.0.1 --port 8000
```

Wait for both terminals to report successful startup before running the check.
The sample contract defines required JSON fields, numeric bounds, an enum,
query and path parameters, and local schema references.

## Verify the flow in terminal three

```powershell
.\.venv\Scripts\python.exe .\examples\demo\check.py
```

On a fresh upstream, the output is:

```text
Accepted request: HTTP 201 (reached upstream)
Rejected request: HTTP 400 (body_validation_failed)
Upstream calls: 0 -> 1 -> 1
The rejected request did not reach upstream.
```

The check creates an order with quantity 2, then submits an order with quantity 0.
It reads the upstream counter directly before and after both calls. Only the
accepted request increments that counter. A failed check exits with code 1;
success exits with code 0. Rerunning the check adds one order and verifies the
same one-request increment, so the absolute counts can be higher than shown.
Avoid concurrent demo traffic while checking exact counter increments.

`GET /_demo/stats` is an upstream-only inspection route. It does not increment
the counter and is intentionally absent from the OpenAPI contract, so trying
that path through SpecGuard returns 404. It is demo instrumentation, not a
SpecGuard metrics or health endpoint.

## Inspect stored orders

```powershell
Invoke-RestMethod http://127.0.0.1:8000/orders?limit=5
Invoke-RestMethod http://127.0.0.1:8000/orders/1
```

These valid GET requests also increment the upstream counter. The upstream
supplies the `draft` status when an order omits it; SpecGuard does not inject
schema defaults. An unknown positive order ID returns the upstream's own 404
response. Invalid quantities and IDs are rejected by SpecGuard before forwarding.

If either default port is occupied, use matching ports in the upstream and proxy
startup commands, then pass their URLs to the checker:

```powershell
.\.venv\Scripts\python.exe .\examples\demo\check.py --proxy-url http://127.0.0.1:8080 --upstream-url http://127.0.0.1:9090
```

Stop both servers with Ctrl+C when finished. This example uses local HTTP and
in-memory storage; it adds no authentication, persistence, or response-schema
validation to SpecGuard.
