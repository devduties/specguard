import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from examples.demo.check import check_demo

ROOT = Path(__file__).parents[1]


@contextmanager
def running_server(arguments, port):
    process = subprocess.Popen(
        [sys.executable, "-m", *arguments, "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        deadline = time.monotonic() + 15
        while True:
            if process.poll() is not None:
                raise AssertionError(f"Demo server exited: {process.communicate()[0]}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise AssertionError("Demo server did not start within 15 seconds.") from None
                time.sleep(0.05)
        yield
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)


def test_documented_demo_commands_forward_valid_requests_and_block_invalid_requests():
    with socket.socket() as upstream_socket, socket.socket() as proxy_socket:
        upstream_socket.bind(("127.0.0.1", 0))
        proxy_socket.bind(("127.0.0.1", 0))
        upstream_port = upstream_socket.getsockname()[1]
        proxy_port = proxy_socket.getsockname()[1]
    upstream_url = f"http://127.0.0.1:{upstream_port}"
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    with (
        running_server(["uvicorn", "examples.demo.upstream:app", "--no-access-log"], upstream_port),
        running_server(
            ["specguard", "--spec", "examples/demo/openapi.yaml", "--upstream", upstream_url],
            proxy_port,
        ),
    ):
        # Repeating the documented check must also work with existing orders.
        for before in (0, 1):
            result = subprocess.run(
                [
                    sys.executable,
                    "examples/demo/check.py",
                    "--proxy-url",
                    proxy_url,
                    "--upstream-url",
                    upstream_url,
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            assert f"Upstream calls: {before} -> {before + 1} -> {before + 1}" in result.stdout
            assert "The rejected request did not reach upstream." in result.stdout

        with httpx.Client(timeout=5, trust_env=False) as client:
            for path in ("/orders?limit=0", "/orders/0"):
                rejected = client.get(proxy_url + path)
                assert rejected.status_code == 400
                assert rejected.json()["error"]["code"] == "parameter_validation_failed"
            assert client.get(proxy_url + "/_demo/stats").status_code == 404
            assert client.get(upstream_url + "/_demo/stats").json() == {"received_requests": 2}

            orders = client.get(proxy_url + "/orders?limit=1")
            assert orders.status_code == 200
            assert orders.json() == [{"id": 2, "name": "coffee", "quantity": 2, "status": "draft"}]
            order = client.get(proxy_url + "/orders/1")
            assert order.status_code == 200
            assert order.json()["id"] == 1
            missing = client.get(proxy_url + "/orders/999")
            assert missing.status_code == 404
            assert missing.json() == {"error": "Order not found."}
            assert client.get(upstream_url + "/_demo/stats").json() == {"received_requests": 5}


@pytest.mark.parametrize("invalid_forwarded", [False, True])
def test_checker_detects_incorrect_forwarding(invalid_forwarded):
    received = 0

    def handle(request):
        nonlocal received
        if request.url.path == "/_demo/stats":
            return httpx.Response(200, json={"received_requests": received})
        if json.loads(request.content)["quantity"] == 2:
            if not invalid_forwarded:
                # A fake success without forwarding must not pass the check.
                return httpx.Response(201, headers={"X-Demo-Upstream": "orders"})
            received += 1
            return httpx.Response(201, headers={"X-Demo-Upstream": "orders"})
        if invalid_forwarded:
            received += 1
        return httpx.Response(400, json={"error": {"code": "body_validation_failed"}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ValueError, match="request counts"):
            check_demo(client, "http://proxy.test", "http://upstream.test")


@pytest.mark.parametrize("payload", [[], {"received_requests": True}, {"received_requests": -1}])
def test_checker_rejects_invalid_upstream_counters(payload):
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        with pytest.raises(ValueError, match="demo request counter"):
            check_demo(client, "http://proxy.test", "http://upstream.test")
