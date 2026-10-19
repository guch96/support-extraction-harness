import json

import pytest
from conftest import ScriptedModel
from langchain_core import exceptions as model_errors
from langchain_core.messages import AIMessage

from harness.runtime import Harness, RunInput
from support_extraction.policy import contract


class UnwrappedOpenAIError(Exception):
    pass


UnwrappedOpenAIError.__module__ = "openai._exceptions"


class UnwrappedHTTPXError(Exception):
    pass


UnwrappedHTTPXError.__module__ = "httpx._exceptions"


CALL_FAILURES = [
    pytest.param(TimeoutError("timed out"), "MODEL_TIMEOUT", id="builtin-timeout"),
    pytest.param(model_errors.ModelTimeoutError("timed out"), "MODEL_TIMEOUT", id="model-timeout"),
    pytest.param(
        model_errors.ModelAuthenticationError("bad key"), "MODEL_CALL_FAILED", id="authentication"
    ),
    pytest.param(
        model_errors.ModelPermissionDeniedError("denied"), "MODEL_CALL_FAILED", id="permission"
    ),
    pytest.param(
        model_errors.ModelConnectionError("unreachable"), "MODEL_CALL_FAILED", id="connection"
    ),
    pytest.param(
        model_errors.ModelRateLimitError("slow down"), "MODEL_CALL_FAILED", id="rate-limit"
    ),
    pytest.param(model_errors.ModelAPIError("provider down"), "MODEL_CALL_FAILED", id="api-error"),
    pytest.param(ConnectionError("unreachable"), "MODEL_CALL_FAILED", id="builtin-connection"),
    pytest.param(UnwrappedOpenAIError("raw provider"), "MODEL_CALL_FAILED", id="openai-module"),
    pytest.param(UnwrappedHTTPXError("raw transport"), "MODEL_CALL_FAILED", id="httpx-module"),
]

CONFIG_ERRORS = [
    pytest.param(
        model_errors.ModelInvalidRequestError("bad request"), id="invalid-request"
    ),
    pytest.param(model_errors.ModelNotFoundError("model missing"), id="not-found"),
]


class MemoryAudit:
    def __init__(self):
        self.attempts = []
        self.reviews = []

    def run_started(self, run):
        pass

    def attempt_started(self, request_id, attempt):
        self.attempts.append({"attempt": attempt})

    def attempt_finished(self, request_id, attempt, raw, latency_ms, error_code):
        self.attempts[attempt - 1].update(raw=raw, latency_ms=latency_ms, error_code=error_code)

    def validation_failed(self, request_id, attempt, feedback):
        self.attempts[attempt - 1]["feedback"] = feedback

    def run_finished(self, result):
        if result.outcome == "FALLBACK":
            self.reviews.append(result.request_id)
            return "review-test"
        return None


def make_harness(responses, audit=None):
    model = ScriptedModel(responses=responses)
    audit = audit or MemoryAudit()
    return Harness(model, contract(), audit), model, audit


def source():
    return RunInput(
        session_id="session-test",
        task_id="task-test",
        request_id="request-test",
        sources=[{"sequence": 1, "kind": "transcript", "text": "重复扣款，退款已发起。"}],
        source_revision=1,
        pii_seen=False,
    )


async def test_first_success(valid_json, valid_data):
    harness, model, audit = make_harness([valid_json])
    result = await harness.run(source())
    assert result.outcome == "SUCCESS"
    assert result.data == valid_data
    assert result.model_calls == 1 and result.repair_count == 0
    assert len(model.calls[0]) == 2
    assert audit.attempts[0]["latency_ms"] >= 0


@pytest.mark.parametrize(
    "bad",
    [
        "not json",
        "```json\n{}\n```",
        "{}",
        '{"issue_category":"WRONG"}',
    ],
)
async def test_repairs_real_middleware(bad, valid_json):
    harness, model, audit = make_harness([bad, valid_json])
    result = await harness.run(source())
    assert result.outcome == "REPAIRED"
    assert result.model_calls == 2 and result.repair_count == 1
    assert len(model.calls[1]) == 3
    assert json.loads(audit.attempts[0]["raw"])["content"] == bad
    assert "repair_feedback_" in model.calls[1][-1].content


async def test_exhaustion_has_durable_review():
    harness, model, audit = make_harness(["bad one", "bad two", "bad three"])
    result = await harness.run(source())
    assert result.outcome == "FALLBACK" and result.data is None
    assert result.review_id == "review-test"
    assert result.model_calls == 3 and result.repair_count == 2
    assert len(audit.attempts) == 3
    assert "bad two" in model.calls[2][-1].content
    assert "bad one" not in model.calls[2][-1].content


async def test_transport_failure_is_not_output_repair():
    harness, model, audit = make_harness([TimeoutError("sensitive exception")])
    result = await harness.run(source())
    assert result.outcome == "FALLBACK" and result.error_code == "MODEL_TIMEOUT"
    assert len(model.calls) == 1 and result.repair_count == 0
    assert audit.attempts[0]["error_code"] == "MODEL_TIMEOUT"


@pytest.mark.parametrize("exc,code", CALL_FAILURES)
async def test_model_call_failures_fall_back_with_a_durable_review(exc, code):
    harness, model, audit = make_harness([exc])
    result = await harness.run(source())
    assert result.outcome == "FALLBACK"
    assert result.data is None
    assert result.error_code == code
    assert result.review_id == "review-test"
    assert result.model_calls == 1 and result.repair_count == 0
    assert len(model.calls) == 1
    assert audit.attempts[0]["error_code"] == code
    assert audit.attempts[0]["latency_ms"] >= 0


@pytest.mark.parametrize("exc", CONFIG_ERRORS)
async def test_model_config_errors_are_service_errors_without_a_review(exc):
    from harness.contracts import ServiceError

    harness, model, audit = make_harness([exc])
    with pytest.raises(ServiceError) as raised:
        await harness.run(source())
    assert raised.value.code == "MODEL_CONFIG_ERROR"
    assert raised.value.request_id == "request-test"
    assert len(model.calls) == 1
    assert audit.reviews == []
    assert audit.attempts[0]["error_code"] == "MODEL_CONFIG_ERROR"
    assert audit.attempts[0]["latency_ms"] >= 0


async def test_unknown_exception_stays_internal_error():
    from harness.contracts import ServiceError

    harness, _, audit = make_harness([RuntimeError("unexpected")])
    with pytest.raises(ServiceError) as raised:
        await harness.run(source())
    assert raised.value.code == "INTERNAL_ERROR"
    assert audit.reviews == []


async def test_unexpected_tool_call_is_never_executed(valid_json):
    message = AIMessage(
        content=valid_json,
        tool_calls=[
            {"name": "refund", "args": {"amount": 999}, "id": "evil"},
        ],
    )
    harness, model, audit = make_harness([message, valid_json])
    result = await harness.run(source())
    assert result.outcome == "REPAIRED"
    assert audit.attempts[0]["feedback"]["errors"][0]["code"] == "unexpected_tool_call"
    assert "refund" not in model.calls[1][-1].content


async def test_review_failure_is_service_error():
    from harness.runtime import ServiceError

    class BrokenAudit(MemoryAudit):
        def run_finished(self, result):
            raise OSError("database failure")

    harness, _, _ = make_harness(["bad"] * 3, BrokenAudit())
    with pytest.raises(ServiceError, match="PERSISTENCE_FAILED"):
        await harness.run(source())


async def test_strict_types_and_length(valid_data, valid_json):
    for field, value in [
        ("detected_pii", "false"),
        ("summary", "长" * 201),
        ("issue_category", "billing"),
        ("customer_id", 123),
    ]:
        bad = json.dumps({**valid_data, field: value})
        harness, _, _ = make_harness([bad, valid_json])
        assert (await harness.run(source())).outcome == "REPAIRED"


async def test_sensitive_feedback_and_unknown_field_names_are_not_leaked(valid_data, caplog):
    bad = json.dumps({**valid_data, "SuperSecretPass123": "密码是 SuperSecretPass123"})
    harness, model, audit = make_harness([bad, json.dumps(valid_data)])
    run = source()
    from dataclasses import replace

    run = replace(run, pii_seen=True, secret_values=("SuperSecretPass123",))
    with caplog.at_level("INFO", logger="support_harness.events"):
        result = await harness.run(run)
    assert result.data["detected_pii"] is True
    assert "SuperSecretPass123" not in str(model.calls)
    assert "SuperSecretPass123" not in caplog.text
    assert "extra_field" in audit.attempts[0]["feedback"]["error_signature"][0]
    assert "SuperSecretPass123" in audit.attempts[0]["raw"]
