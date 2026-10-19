import json
import logging
from typing import Any

from harness.contracts import RunInput

logger = logging.getLogger("support_harness.events")


def event(name: str, **fields: Any) -> None:
    logger.info(json.dumps({"event": name, **fields}, ensure_ascii=False))


def correlation(run: RunInput) -> dict[str, Any]:
    return {
        "session_id": run.session_id,
        "task_id": run.task_id,
        "request_id": run.request_id,
        "source_revision": run.source_revision,
    }
