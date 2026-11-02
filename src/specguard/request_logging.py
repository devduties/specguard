"""JSON request records and CLI logging configuration."""

import json
import logging
from copy import deepcopy
from datetime import UTC, datetime

from uvicorn.config import LOGGING_CONFIG


class RequestFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Only these fields belong in request logs; never serialize arbitrary extras.
        return json.dumps(
            {
                "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
                "level": record.levelname.lower(),
                "event": "request_finished",
                "request_id": record.request_id,
                "method": record.method,
                "route": record.route,
                "status_code": record.status_code,
                "duration_ms": record.duration_ms,
                "outcome": record.outcome,
                "error_code": record.error_code,
            },
            separators=(",", ":"),
        )


def logging_config() -> dict:
    config = deepcopy(LOGGING_CONFIG)
    config["formatters"]["request"] = {"()": RequestFormatter}
    config["handlers"]["request"] = {
        "class": "logging.StreamHandler",
        "formatter": "request",
        "stream": "ext://sys.stderr",
    }
    config["loggers"]["specguard.requests"] = {
        "handlers": ["request"],
        "level": "INFO",
        "propagate": False,
    }
    return config
