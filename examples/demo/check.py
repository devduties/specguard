"""Show one accepted request reaching upstream and one invalid request blocked."""

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass

import httpx


@dataclass(frozen=True, slots=True)
class DemoResult:
    accepted_status: int
    rejected_status: int
    before: int
    after_accepted: int
    after_rejected: int


def check_demo(client: httpx.Client, proxy_url: str, upstream_url: str) -> DemoResult:
    proxy_url = proxy_url.rstrip("/")
    upstream_url = upstream_url.rstrip("/")
    before = _received_count(client, upstream_url)
    accepted = client.post(f"{proxy_url}/orders", json={"name": "coffee", "quantity": 2})
    if accepted.status_code != 201 or accepted.headers.get("x-demo-upstream") != "orders":
        raise ValueError(
            f"Expected HTTP 201 from the demo upstream; received {accepted.status_code}."
        )
    after_accepted = _received_count(client, upstream_url)
    rejected = client.post(f"{proxy_url}/orders", json={"name": "coffee", "quantity": 0})
    payload = rejected.json()
    error = payload.get("error") if isinstance(payload, dict) else None
    if (
        rejected.status_code != 400
        or not isinstance(error, dict)
        or error.get("code") != "body_validation_failed"
    ):
        raise ValueError("Expected a body-validation error from SpecGuard for quantity=0.")
    after_rejected = _received_count(client, upstream_url)
    if after_accepted != before + 1 or after_rejected != after_accepted:
        raise ValueError("Upstream request counts did not show exactly one accepted request.")
    return DemoResult(
        accepted.status_code, rejected.status_code, before, after_accepted, after_rejected
    )


def _received_count(client: httpx.Client, upstream_url: str) -> int:
    result = client.get(f"{upstream_url}/_demo/stats")
    result.raise_for_status()
    payload = result.json()
    count = payload.get("received_requests") if isinstance(payload, dict) else None
    if type(count) is not int or count < 0:
        raise ValueError("The upstream did not return the demo request counter.")
    return count


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy-url", default="http://127.0.0.1:8000")
    parser.add_argument("--upstream-url", default="http://127.0.0.1:9000")
    args = parser.parse_args(argv)
    try:
        with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
            result = check_demo(client, args.proxy_url, args.upstream_url)
    except (httpx.HTTPError, ValueError) as exc:
        print(f"Demo check failed: {exc}", file=sys.stderr)
        return 1
    print(f"Accepted request: HTTP {result.accepted_status} (reached upstream)")
    print(f"Rejected request: HTTP {result.rejected_status} (body_validation_failed)")
    print(f"Upstream calls: {result.before} -> {result.after_accepted} -> {result.after_rejected}")
    print("The rejected request did not reach upstream.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
