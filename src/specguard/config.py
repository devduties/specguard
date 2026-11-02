"""Command-line and environment configuration for the proxy server."""

import argparse
import ipaddress
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from specguard.forwarding import ProxySettings


@dataclass(frozen=True, slots=True)
class ServerSettings:
    spec_path: Path
    proxy: ProxySettings
    host: str = "127.0.0.1"
    port: int = 8000

    def __post_init__(self) -> None:
        if not self.host or any(c.isspace() or ord(c) < 32 or c in "/\\@?#" for c in self.host):
            raise ValueError("host must be an IP address or hostname without a URL scheme.")
        if ":" in self.host:
            try:
                ipaddress.IPv6Address(self.host)
            except ValueError as exc:
                raise ValueError("Use --port separately from the listen host.") from exc
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535.")


def parse_settings(
    argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None
) -> ServerSettings:
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(
        prog="specguard", description="Validate and proxy OpenAPI requests."
    )
    parser.add_argument(
        "--spec",
        type=Path,
        default=env.get("SPECGUARD_SPEC") or None,
        help="Local OpenAPI JSON/YAML file (SPECGUARD_SPEC).",
    )
    parser.add_argument(
        "--upstream",
        default=env.get("SPECGUARD_UPSTREAM"),
        help="Upstream HTTP(S) base URL (SPECGUARD_UPSTREAM).",
    )
    parser.add_argument(
        "--host",
        default=env.get("SPECGUARD_HOST", "127.0.0.1"),
        help="Listen address (SPECGUARD_HOST; default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=env.get("SPECGUARD_PORT", "8000"),
        help="Listen port (SPECGUARD_PORT; default: 8000).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=env.get("SPECGUARD_TIMEOUT", "10"),
        help="Upstream timeout in seconds (SPECGUARD_TIMEOUT; default: 10).",
    )
    parser.add_argument(
        "--max-request-bytes",
        type=int,
        default=env.get("SPECGUARD_MAX_REQUEST_BYTES", str(1024 * 1024)),
        help="Request body cap (SPECGUARD_MAX_REQUEST_BYTES; default: 1048576).",
    )
    parser.add_argument(
        "--max-response-bytes",
        type=int,
        default=env.get("SPECGUARD_MAX_RESPONSE_BYTES", str(8 * 1024 * 1024)),
        help="Response body cap (SPECGUARD_MAX_RESPONSE_BYTES; default: 8388608).",
    )
    args = parser.parse_args(argv)
    if args.spec is None or not args.upstream:
        parser.error("Provide --spec and --upstream, or their environment variables.")
    try:
        proxy = ProxySettings(
            args.upstream, args.timeout, args.max_request_bytes, args.max_response_bytes
        )
        return ServerSettings(args.spec, proxy, args.host, args.port)
    except ValueError as exc:
        parser.error(str(exc))
