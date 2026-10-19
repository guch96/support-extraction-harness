import json
import logging
import os
import sys

import uvicorn

from entrypoints.api import create_app

HOST = "127.0.0.1"
DEFAULT_PORT = 8000
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


def main():
    configure_logging()
    port = int(os.getenv("HARNESS_PORT", str(DEFAULT_PORT)))
    uvicorn.run(create_app(ui=True), host=HOST, port=port)


if __name__ == "__main__":
    main()
