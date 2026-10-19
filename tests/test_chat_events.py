import json

from conftest import ScriptedModel

from streaming.events import Phase
from streaming.phase_sink import PhaseSink
from support_extraction.service import ExtractionService, Submission


class RecordSource:
    def __init__(self, text):
        self.text = text
        self.reads = []

    async def read(self, record_id):
        self.reads.append(record_id)
        return self.text


def labels(sink):
    return [chat_event.label for chat_event in sink._events]


async def test_record_mode_reports_reading_then_model_phases(tmp_path, valid_json):
    source = RecordSource("重复扣款，退款已发起。")
    model = ScriptedModel(responses=[valid_json])
    sink = PhaseSink()
    app = await ExtractionService.open(
        tmp_path, lambda: model, source_reader=source, allowed_records={"TX-001"}
    )
    try:
        result = await app.submit(Submission(record_id="TX-001"), phases=sink)
    finally:
        await app.close()

    assert result.outcome == "SUCCESS"
    assert [chat_event.phase for chat_event in sink._events] == [
        Phase.READING,
        Phase.CALLING_MODEL,
        Phase.VALIDATING,
        Phase.SUCCEEDED,
    ]
    assert labels(sink) == [
        "正在读取预置记录",
        "正在调用模型",
        "正在校验输出",
        "提取完成",
    ]
    assert [chat_event.seq for chat_event in sink._events] == [1, 2, 3, 4]


async def test_repair_run_reports_each_model_call_and_validation(tmp_path, valid_json):
    model = ScriptedModel(responses=["bad", valid_json])
    sink = PhaseSink()
    app = await ExtractionService.open(tmp_path, lambda: model)
    try:
        result = await app.submit(Submission(text="重复扣款。"), phases=sink)
    finally:
        await app.close()

    assert result.outcome == "REPAIRED"
    assert [chat_event.phase for chat_event in sink._events] == [
        Phase.CALLING_MODEL,
        Phase.VALIDATING,
        Phase.REPAIRING,
        Phase.CALLING_MODEL,
        Phase.VALIDATING,
        Phase.SUCCEEDED,
    ]
    assert labels(sink)[2] == "正在修复"

async def test_exhausted_run_ends_failed_without_leaking_failure_details(tmp_path):
    secret = "SuperSecretPass123"
    model = ScriptedModel(
        responses=[
            f"Tool refund called with {secret} and NRIC S1234567D",
            "raw provider failure",
            "another raw failure",
        ]
    )
    sink = PhaseSink()
    app = await ExtractionService.open(tmp_path, lambda: model)
    try:
        result = await app.submit(
            Submission(text=f"密码是 {secret}，支付崩溃。"), phases=sink
        )
    finally:
        await app.close()

    assert result.outcome == "FALLBACK" and result.review_id
    assert [chat_event.phase for chat_event in sink._events] == [
        Phase.CALLING_MODEL,
        Phase.VALIDATING,
        Phase.REPAIRING,
        Phase.CALLING_MODEL,
        Phase.VALIDATING,
        Phase.REPAIRING,
        Phase.CALLING_MODEL,
        Phase.VALIDATING,
        Phase.FAILED,
    ]
    assert labels(sink)[-1] == "未取得合格结果，已转人工审核"
    joined = "".join(labels(sink))
    for raw in (secret, "S1234567D", "refund", "raw provider failure", "another raw failure"):
        assert raw not in joined


async def test_direct_text_never_reports_reading(tmp_path, valid_json):
    model = ScriptedModel(responses=[valid_json])
    sink = PhaseSink()
    app = await ExtractionService.open(tmp_path, lambda: model)
    try:
        await app.submit(Submission(text="重复扣款。"), phases=sink)
    finally:
        await app.close()

    assert Phase.READING not in [chat_event.phase for chat_event in sink._events]
    assert labels(sink) == ["正在调用模型", "正在校验输出", "提取完成"]


async def test_pii_values_stay_out_of_phase_labels_and_history_sources(tmp_path, valid_json):
    password, nric = "SuperSecretPass123", "S1234567D"
    model = ScriptedModel(responses=[json.dumps({**json.loads(valid_json), "detected_pii": True})])
    sink = PhaseSink()
    app = await ExtractionService.open(tmp_path, lambda: model)
    try:
        result = await app.submit(
            Submission(text=f"密码是 {password}，NRIC {nric}。支付失败。"), phases=sink
        )
        history = await app.history(result.session_id)
    finally:
        await app.close()

    assert result.outcome == "SUCCESS"
    assert labels(sink) == ["正在调用模型", "正在校验输出", "提取完成"]
    assert password not in "".join(labels(sink)) and nric not in "".join(labels(sink))
    task = history[0]
    assert task["pii_seen"] is True
    stored = "".join(source["text"] for source in task["sources"])
    assert password not in stored and nric not in stored
    assert "[REDACTED_PII]" in stored
