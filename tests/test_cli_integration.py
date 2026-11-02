import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx


def test_cli_launches_a_working_validation_proxy(tmp_path):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_args):
            pass

    document = {
        "openapi": "3.1.0",
        "info": {"title": "CLI test", "version": "1"},
        "paths": {
            "/items/{id}": {
                "get": {
                    "parameters": [
                        {
                            "name": "id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer", "minimum": 1},
                        }
                    ],
                    "responses": {"200": {"description": "OK"}},
                }
            },
        },
    }
    spec = tmp_path / "api.json"
    spec.write_text(json.dumps(document), encoding="utf-8")
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    command = [
        sys.executable,
        "-m",
        "specguard",
        "--spec",
        str(spec),
        "--upstream",
        f"http://127.0.0.1:{upstream.server_port}/api",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--timeout",
        "2",
        "--max-request-bytes",
        "128",
        "--max-response-bytes",
        "1024",
    ]
    process = None
    try:
        process = subprocess.Popen(
            command,
            cwd=Path(__file__).parents[1],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        deadline = time.monotonic() + 15
        while True:
            if process.poll() is not None:
                raise AssertionError(f"CLI exited during startup: {process.communicate()[0]}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise AssertionError("CLI did not start listening within 15 seconds.") from None
                time.sleep(0.05)

        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", timeout=3, trust_env=False
        ) as client:
            accepted = client.get("/items/2?tag=a%2Fb&tag=a+b")
            rejected = client.get("/items/0")
            missing = client.get("/missing")
            oversized = client.post("/items/1", content=b"x" * 129)
        assert accepted.status_code == 200
        assert accepted.content == b"ok"
        assert rejected.status_code == 400
        assert rejected.json()["error"]["code"] == "parameter_validation_failed"
        assert missing.status_code == 404
        assert oversized.status_code == 413
        assert received == ["/api/items/2?tag=a%2Fb&tag=a+b"]
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()
            try:
                output = process.communicate(timeout=5)[0]
            except subprocess.TimeoutExpired:
                process.kill()
                output = process.communicate(timeout=5)[0]
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=5)

    events = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    assert len(events) == 4
    assert {event["status_code"] for event in events} == {200, 400, 404, 413}
    assert events[0]["request_id"] == accepted.headers["x-request-id"]
    assert events[1]["request_id"] == rejected.headers["x-request-id"]
    assert events[0]["route"] == "/items/{id}"
    assert events[0]["method"] == "get"
    assert events[0]["event"] == "request_finished"
    assert events[0]["level"] == "info"
    assert all("tag=" not in json.dumps(event) for event in events)
