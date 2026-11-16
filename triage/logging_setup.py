"""Structured JSON logging. Libraries don't configure logging; only the CLI/server entry points call
configure(). Log events carry ids and numbers, never prompts, completions or credentials."""
from __future__ import annotations

import json
import logging
import sys
import time


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            **getattr(record, "fields", {}),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure(level: str = "INFO", stream=None) -> None:
    root = logging.getLogger("triage")
    root.setLevel(level)
    if not any(getattr(h, "_triage", False) for h in root.handlers):
        handler = logging.StreamHandler(stream or sys.stderr)
        handler.setFormatter(JsonFormatter())
        handler._triage = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.propagate = False


def event(logger: logging.Logger, level: int, name: str, **fields) -> None:
    logger.log(level, name, extra={"fields": fields})
