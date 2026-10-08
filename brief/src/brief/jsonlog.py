"""Structured JSON logs to stdout: one object per line, so `docker logs` can be searched and parsed.

Logs are scrubbed: any text field is passed through `redact.scrub`, so an IP address, a MAC, an e-mail
address, a credential in a URL, or a long secret never reaches `docker logs` by accident (a config error
message that quotes a host, an exception that carries an address). Call sites also leave out the fields
that are names (hosts, titles, headlines). The one exception is a message the person has to read to act on,
logged with `raw=True` (the one-time setup URL).
"""

import json
import logging
import sys
from datetime import UTC, datetime

from brief.redact import scrub


def log(event: str, raw: bool = False, **fields) -> None:
    if not raw:
        fields = {k: scrub(v) if isinstance(v, str) else v for k, v in fields.items()}
    record = {"ts": datetime.now(UTC).isoformat(), "event": event, **fields}
    print(json.dumps(record), flush=True)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "event": "log",
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": scrub(record.getMessage()),
        }
        if record.exc_info:
            out["error"] = scrub(self.formatException(record.exc_info))
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
