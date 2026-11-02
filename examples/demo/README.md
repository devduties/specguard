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

The upstream stores orders in memory and leaves contract validation to SpecGuard.
Everything resets when this process restarts.

## Start SpecGuard in terminal two

```powershell
.\.venv\Scripts\python.exe -m specguard --spec .\examples\demo\openapi.yaml --upstream http://127.0.0.1:9000 --host 127.0.0.1 --port 8000
```

Wait for both terminals to report successful startup before running the check.
The sample contract covers required JSON fields, numeric bounds, an enum,
query and path parameters, and internal schema references.

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

The check creates an order with quantity 2, then tries one with quantity 0.
It reads the upstream request counter before and after both calls. Only the
valid order increments it. A failed check exits with code 1; success exits with
code 0. If you run the check more than once, the counter will be higher but the
increment from each run is always exactly one.

`GET /_demo/stats` is a direct upstream route used to read the counter. It's not
in the OpenAPI contract, so going through SpecGuard returns 404. Don't mix demo
traffic from other sources while checking, or the counter won't match.

## Inspect stored orders

```powershell
Invoke-RestMethod http://127.0.0.1:8000/orders?limit=5
Invoke-RestMethod http://127.0.0.1:8000/orders/1
```

These GET requests go through SpecGuard and also increment the upstream counter.
The upstream fills in `draft` as the default status when an order doesn't include
one. SpecGuard doesn't inject defaults itself. An unknown order ID returns whatever
the upstream sends back. Invalid quantities and IDs are caught by SpecGuard before
the request is forwarded.

If either default port is taken, use different ports in the startup commands and
pass them to the checker:

```powershell
.\.venv\Scripts\python.exe .\examples\demo\check.py --proxy-url http://127.0.0.1:8080 --upstream-url http://127.0.0.1:9090
```

Stop both servers with Ctrl+C when you're done. This demo runs entirely on your
machine with no external services.
