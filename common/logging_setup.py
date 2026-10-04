"""Structured logging: one JSON-ish stream on stderr plus readable console lines."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from common.config import get_settings

_CONFIGURED = False


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    level = getattr(logging, get_settings().log_level, logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)-28s %(message)s", "%H:%M:%S")
    )
    root.addHandler(stream)

    file_handler = logging.FileHandler(get_settings().runs_dir / "aiworker.log")
    file_handler.setFormatter(JsonLineFormatter())
    root.addHandler(file_handler)

    for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "playwright"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)


def log_event(logger: logging.Logger, level: int, msg: str, **fields: Any) -> None:
    logger.log(level, msg, extra={"extra_fields": fields})


def setup_logging(level: str = "INFO") -> None:
    """Configure logging once, honouring an explicit level."""
    configure_logging()
    import logging

    logging.getLogger().setLevel(getattr(logging, str(level).upper(), logging.INFO))
    get_logger(__name__).debug("logging configured at %s", level)
