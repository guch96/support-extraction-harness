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
    monkeypatch.delenv("HARNESS_MAX_MESSAGE_CHARS", raising=False)

    serve.main()

    assert seen == {"ui": True, "session_ttl_days": 7, "max_message_chars": 1000}


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


@pytest.mark.parametrize(("configured", "expected"), [(None, 1000), ("2500", 2500)])
def test_serve_reads_the_configured_message_limit(monkeypatch, configured, expected):
    seen = {}
    monkeypatch.setattr(serve, "configure_logging", lambda: None)
    monkeypatch.setattr(
        serve, "create_app", lambda **options: seen.update(options) or object()
    )
    monkeypatch.setattr(serve.uvicorn, "run", lambda app, **options: None)
    if configured is None:
        monkeypatch.delenv("HARNESS_MAX_MESSAGE_CHARS", raising=False)
    else:
        monkeypatch.setenv("HARNESS_MAX_MESSAGE_CHARS", configured)

    serve.main()

    assert seen["max_message_chars"] == expected


@pytest.mark.parametrize("configured", ["0", "-5", "many"])
def test_serve_rejects_an_invalid_message_limit_before_starting(monkeypatch, configured):
    started = []
    monkeypatch.setattr(serve, "configure_logging", lambda: None)
    monkeypatch.setattr(serve, "create_app", lambda **options: object())
    monkeypatch.setattr(
        serve.uvicorn, "run", lambda app, **options: started.append(options)
    )
    monkeypatch.setenv("HARNESS_MAX_MESSAGE_CHARS", configured)

    with pytest.raises(ValueError, match="HARNESS_MAX_MESSAGE_CHARS"):
        serve.main()

    assert started == []
