"""Structured JSON logs to stdout: one object per line, so `docker logs` can be searched and parsed."""

import json
import logging
import sys
from datetime import UTC, datetime


def log(event: str, **fields) -> None:
    record = {"ts": datetime.now(UTC).isoformat(), "event": event, **fields}
    print(json.dumps(record), flush=True)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "event": "log",
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            out["error"] = self.formatException(record.exc_info)
        return json.dumps(out)


def uvicorn_log_config() -> dict:
    """A logging config for Uvicorn that prints JSON lines instead of its plain-text format."""
    handler = {"class": "logging.StreamHandler", "formatter": "json", "stream": sys.stdout}
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"json": {"()": "brief.jsonlog.JsonFormatter"}},
        "handlers": {"default": handler},
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"level": "INFO"},
            "uvicorn.access": {"handlers": ["default"], "level": "WARNING", "propagate": False},
        },
    }
