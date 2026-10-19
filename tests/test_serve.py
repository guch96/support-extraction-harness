import io
import json
import logging

import pytest

from entrypoints import serve
from runtime.events import event


def test_configure_logging_emits_one_json_object_per_line():
    stream = io.StringIO()
    handler = serve.configure_logging(stream=stream)
    logger = logging.getLogger("support_harness.events")
    try:
        event("model_call", request_id="req-1", attempt=1)
        logger.info("plain text")
        lines = stream.getvalue().splitlines()
    finally:
        logger.removeHandler(handler)

    assert len(lines) == 2
    assert json.loads(lines[0]) == {"event": "model_call", "request_id": "req-1", "attempt": 1}
    plain = json.loads(lines[1])
    assert plain["event"] == "log" and plain["message"] == "plain text"


@pytest.mark.parametrize(("configured", "expected"), [(None, 8000), ("8123", 8123)])
def test_serve_binds_loopback_with_configured_port(monkeypatch, configured, expected):
    seen = {}
    monkeypatch.setattr(serve, "configure_logging", lambda: None)
    monkeypatch.setattr(serve, "create_app", lambda **options: object())
    monkeypatch.setattr(serve.uvicorn, "run", lambda app, **options: seen.update(options))
    if configured is None:
        monkeypatch.delenv("HARNESS_PORT", raising=False)
    else:
        monkeypatch.setenv("HARNESS_PORT", configured)

    serve.main()

    assert seen["host"] == "127.0.0.1"
    assert seen["port"] == expected


def test_serve_builds_the_app_with_the_review_page(monkeypatch):
    seen = {}
    monkeypatch.setattr(serve, "configure_logging", lambda: None)
    monkeypatch.setattr(
        serve, "create_app", lambda **options: seen.update(options) or object()
    )
    monkeypatch.setattr(serve.uvicorn, "run", lambda app, **options: None)
    monkeypatch.delenv("HARNESS_SESSION_TTL_DAYS", raising=False)

    serve.main()

    assert seen == {"ui": True, "session_ttl_days": 7}


@pytest.mark.parametrize(("configured", "expected"), [(None, 7), ("3", 3)])
def test_serve_reads_the_configured_session_ttl_days(monkeypatch, configured, expected):
    seen = {}
    monkeypatch.setattr(serve, "configure_logging", lambda: None)
    monkeypatch.setattr(
        serve, "create_app", lambda **options: seen.update(options) or object()
    )
    monkeypatch.setattr(serve.uvicorn, "run", lambda app, **options: None)
    if configured is None:
        monkeypatch.delenv("HARNESS_SESSION_TTL_DAYS", raising=False)
    else:
        monkeypatch.setenv("HARNESS_SESSION_TTL_DAYS", configured)

    serve.main()

    assert seen["session_ttl_days"] == expected
