import json
import logging
import os
import sys

import uvicorn

from entrypoints.api import create_app
from support_extraction.service import positive_int

HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_MAX_MESSAGE_CHARS = 1000
EVENT_LOGGER = "support_harness.events"
HANDLER_NAME = "support_harness.json"


class JsonLineFormatter(logging.Formatter):
    """Render each record as one JSON object, one physical line per record."""

    def format(self, record):
        message = record.getMessage()
        try:
            payload = json.loads(message)
        except (TypeError, ValueError):
            payload = None
        if not isinstance(payload, dict):
            payload = {
                "event": "log",
                "level": record.levelname,
                "logger": record.name,
                "message": message,
            }
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(stream=None):
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.name = HANDLER_NAME
    handler.setFormatter(JsonLineFormatter())
    logger = logging.getLogger(EVENT_LOGGER)
    for existing in list(logger.handlers):
        if existing.name == HANDLER_NAME:
            logger.removeHandler(existing)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return handler


def env_positive_int(name, default):
    raw = os.getenv(name, str(default))
    try:
        return positive_int(name, int(raw))
    except ValueError:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}") from None


def main():
    configure_logging()
    port = int(os.getenv("HARNESS_PORT", str(DEFAULT_PORT)))
    session_ttl_days = int(os.getenv("HARNESS_SESSION_TTL_DAYS", "7"))
    max_message_chars = env_positive_int(
        "HARNESS_MAX_MESSAGE_CHARS", DEFAULT_MAX_MESSAGE_CHARS
    )
    uvicorn.run(
        create_app(
            ui=True,
            session_ttl_days=session_ttl_days,
            max_message_chars=max_message_chars,
        ),
        host=HOST,
        port=port,
    )


if __name__ == "__main__":
    main()
