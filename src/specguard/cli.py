"""Launch the SpecGuard HTTP server."""

import sys
from collections.abc import Sequence

import uvicorn

from specguard.config import parse_settings
from specguard.server import create_app
from specguard.spec_loader import SpecLoadError


def main(argv: Sequence[str] | None = None) -> int:
    settings = parse_settings(argv)
    try:
        app = create_app(settings)
    except SpecLoadError as exc:
        print(f"specguard: cannot load contract: {exc}", file=sys.stderr)
        return 2
    try:
        uvicorn.run(
            app,
            host=settings.host,
            port=settings.port,
            lifespan="on",
            workers=1,
            access_log=False,
            proxy_headers=False,
            server_header=False,
            date_header=False,
            ws="none",
        )
    except OSError as exc:
        print(f"specguard: cannot start listener: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0
