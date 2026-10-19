import asyncio
import json
from dataclasses import replace

import pytest
from conftest import ScriptedModel
from test_harness import make_harness, source

from harness.prompts import EXTRACTION_PROMPT_VERSION, REPAIR_PROMPT_VERSION
from support_extraction.service import ExtractionService, InputError, Submission

EVENT_LOGGER = "support_harness.events"


def events(caplog):
    return [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == EVENT_LOGGER
    ]


def only(records, name):
    matches = [record for record in records if record["event"] == name]
    assert len(matches) == 1, matches
    return matches[0]


async def test_model_events_correlate_run_counts_and_prompt_versions(valid_json, caplog):
    with caplog.at_level("INFO", logger=EVENT_LOGGER):
        harness, model, audit = make_harness(["not json", valid_json])
        result = await harness.run(source())

    records = events(caplog)
    calls = [record for record in records if record["event"] == "model_call"]
    assert [call["attempt"] for call in calls] == [1, 2]
    expected = {
        "session_id": "session-test",
        "task_id": "task-test",
        "request_id": "request-test",
        "source_revision": 1,
    }
    for call in calls:
        assert {key: call[key] for key in expected} == expected
        assert call["model_calls"] == call["attempt"]
        assert call["repair_count"] == call["attempt"] - 1

    assert calls[0]["prompt_version"] == "extraction.v1" == EXTRACTION_PROMPT_VERSION
    assert calls[1]["prompt_version"] == "repair.v1" == REPAIR_PROMPT_VERSION

    failed = only(records, "validation_failed")
    assert failed["attempt"] == 1
    assert failed["request_id"] == "request-test"
    assert failed["source_revision"] == 1
    assert failed["error_signature"]

    finished = only(records, "run_finished")
    assert finished["outcome"] == result.outcome == "REPAIRED"
    assert finished["model_calls"] == result.model_calls == 2
    assert finished["repair_count"] == result.repair_count == 1
    assert finished["source_revision"] == 1


async def test_sdk_and_wrapper_latencies_are_separate_millisecond_measurements(
    valid_json, caplog
):
    async def slow_response():
        await asyncio.sleep(0.05)
        return valid_json

    harness, model, audit = make_harness([slow_response])
    with caplog.at_level("INFO", logger=EVENT_LOGGER):
        await harness.run(source())

    call = only(events(caplog), "model_call")
    assert 40 <= call["sdk_latency_ms"] < 5000
    assert call["wrapper_latency_ms"] >= call["sdk_latency_ms"]
    assert "latency_ms" not in call


class SlowRecordSource:
    def __init__(self, text):
        self.text = text
        self.reads = []

    async def read(self, record_id):
        self.reads.append(record_id)
        await asyncio.sleep(0.02)
        return self.text


async def test_source_read_and_denial_events_carry_reason_and_timing(
    tmp_path, valid_json, caplog
):
    reader = SlowRecordSource("重复扣款，退款已发起。我的密码是 EventSecret987。")
    model = ScriptedModel(responses=[valid_json])
    app = await ExtractionService.open(
        tmp_path, lambda: model, source_reader=reader, allowed_records={"TX-001"}
    )
    try:
        with caplog.at_level("INFO", logger=EVENT_LOGGER):
            result = await app.submit(Submission(record_id="TX-001"))
            with pytest.raises(InputError, match="ACCESS_DENIED"):
                await app.submit(Submission(record_id="TX-002"))
        persisted = (await app.restore(result.session_id))["runs"][-1]
    finally:
        await app.close()

    records = events(caplog)
    read = only(records, "source_read")
    assert read["reason"] == "ACCESS_GRANTED"
    assert read["outcome"] == "RECEIVED"
    assert "error_code" not in read
    assert 15 <= read["latency_ms"] < 5000

    denied = only(records, "source_denied")
    assert denied["reason"] == "ACCESS_DENIED"
    assert denied["latency_ms"] >= 0
    assert read["request_id"] != denied["request_id"]
    assert all("tool_call_id" not in record for record in records)
    assert reader.reads == ["TX-001"]
    assert "EventSecret987" not in caplog.text

    finished = only(records, "run_finished")
    assert finished["outcome"] == result.outcome == "SUCCESS"
    assert persisted["status"] == finished["outcome"]
    assert persisted["source_revision"] == finished["source_revision"] == 1


async def test_event_lines_are_single_json_objects_without_sensitive_values(
    valid_data, caplog
):
    secret = "EventSecret987"
    leaked = json.dumps({**valid_data, "SuperSecretPass123": f"密码是 {secret}"})
    clean = json.dumps(valid_data, ensure_ascii=False)
    harness, model, audit = make_harness([leaked, clean])
    run = replace(source(), pii_seen=True, secret_values=(secret,))

    with caplog.at_level("INFO", logger=EVENT_LOGGER):
        result = await harness.run(run)

    assert result.outcome == "REPAIRED"
    lines = [
        record.getMessage() for record in caplog.records if record.name == EVENT_LOGGER
    ]
    assert lines
    for line in lines:
        assert "\n" not in line
        assert secret not in line
        assert isinstance(json.loads(line), dict)


async def test_fallback_event_matches_the_persisted_review(tmp_path, caplog):
    model = ScriptedModel(responses=["bad one", "bad two", "bad three"])
    app = await ExtractionService.open(tmp_path, lambda: model)
    try:
        with caplog.at_level("INFO", logger=EVENT_LOGGER):
            result = await app.submit(Submission(text="重复扣款，退款已发起。"))
        review = await app.review(result.review_id)
    finally:
        await app.close()

    finished = only(events(caplog), "run_finished")
    assert finished["outcome"] == result.outcome == "FALLBACK"
    assert finished["review_id"] == result.review_id == review["review_id"]
    assert finished["model_calls"] == review["model_calls"] == 3
    assert finished["repair_count"] == review["repair_count"] == 2
    assert finished["source_revision"] == review["source_revision"] == 1
