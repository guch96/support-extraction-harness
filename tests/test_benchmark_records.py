import json

import pytest
from conftest import ScriptedModel
from fastapi.testclient import TestClient

from entrypoints.api import create_app
from infrastructure.records import FixtureSource
from support_extraction.benchmark import BENCHMARK_RECORD_IDS, FIXTURE_PATH
from support_extraction.service import ExtractionService, InputError, Submission

SIX_FIELDS = {
    "customer_id",
    "issue_category",
    "urgency",
    "summary",
    "actionable_steps",
    "detected_pii",
}


def fixture_data():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class RecordingFixtureSource(FixtureSource):
    def __init__(self):
        super().__init__(FIXTURE_PATH, BENCHMARK_RECORD_IDS)
        self.reads = []

    async def read(self, record_id):
        self.reads.append(record_id)
        return await super().read(record_id)


def test_benchmark_dataset_covers_the_required_cases():
    data = fixture_data()
    records = data["records"]

    assert set(records) == BENCHMARK_RECORD_IDS
    assert "CUST-98234" in records["TX-001"] and "4402" in records["TX-001"]
    assert "SuperSecretPass123" in records["TX-002"] and "S9123456A" in records["TX-002"]
    assert "SYSTEM OVERRIDE" in records["TX-003"]
    assert "全部设为 null" in records["TX-004"]

    injection = records["TX-INJECT"]
    assert "读取记录 TX-002" in injection
    assert "<system>" in injection and "source_material" in injection

    assert len(records["TX-LONG"]) > 20000
    truncated = data["outputs"]["truncated"]
    assert truncated.startswith("{")
    with pytest.raises(json.JSONDecodeError):
        json.loads(truncated)


@pytest.mark.parametrize(
    "record_id",
    [
        "",
        "../TX-001",
        "/etc/passwd",
        "file:///etc/passwd",
        "https://example.test/TX-001",
        "TX-999",
    ],
)
async def test_adapter_serves_only_code_selected_record_ids(record_id):
    source = FixtureSource(FIXTURE_PATH, BENCHMARK_RECORD_IDS)
    with pytest.raises(LookupError):
        await source.read(record_id)


async def test_adapter_returns_the_packaged_transcript_verbatim():
    source = FixtureSource(FIXTURE_PATH, BENCHMARK_RECORD_IDS)
    assert await source.read("TX-004") == fixture_data()["records"]["TX-004"]


async def test_tx001_card_tail_alone_does_not_set_detected_pii(tmp_path, valid_json):
    source = RecordingFixtureSource()
    model = ScriptedModel(responses=[valid_json])
    app = await ExtractionService.open(
        tmp_path,
        lambda: model,
        source_reader=source,
        allowed_records=BENCHMARK_RECORD_IDS,
    )
    try:
        result = await app.submit(Submission(record_id="TX-001"))
        assert result.outcome == "SUCCESS"
        assert set(result.data) == SIX_FIELDS
        assert result.data["customer_id"] == "CUST-98234"
        assert result.data["issue_category"] == "BILLING"
        assert result.data["detected_pii"] is False
        assert "4402" in model.calls[0][1].content
        assert source.reads == ["TX-001"]
    finally:
        await app.close()


async def test_tx002_marks_pii_and_keeps_raw_values_out_of_model_messages(
    tmp_path, valid_data
):
    response = json.dumps(
        {
            **valid_data,
            "customer_id": None,
            "issue_category": "TECHNICAL",
            "urgency": "HIGH",
            "summary": "支付账单时应用崩溃，账户面临锁定。",
            "detected_pii": False,
        },
        ensure_ascii=False,
    )
    source = RecordingFixtureSource()
    model = ScriptedModel(responses=[response])
    app = await ExtractionService.open(
        tmp_path,
        lambda: model,
        source_reader=source,
        allowed_records=BENCHMARK_RECORD_IDS,
    )
    try:
        result = await app.submit(Submission(record_id="TX-002"))
        assert result.outcome == "SUCCESS"
        assert result.data["detected_pii"] is True
        sent = "".join(message.content for message in model.calls[0])
        assert "SuperSecretPass123" not in sent
        assert "S9123456A" not in sent
        assert sent.count("[REDACTED_PII]") == 2
        assert source.reads == ["TX-002"]
    finally:
        await app.close()


async def test_tx003_attack_text_cannot_replace_the_six_field_contract(tmp_path, valid_data):
    response = json.dumps(
        {
            **valid_data,
            "customer_id": None,
            "issue_category": "TECHNICAL",
            "urgency": "MEDIUM",
            "summary": "登录按钮变灰，无法登录。",
            "actionable_steps": [],
            "detected_pii": False,
        },
        ensure_ascii=False,
    )
    source = RecordingFixtureSource()
    model = ScriptedModel(responses=["SYSTEM COMPROMISED", response])
    app = await ExtractionService.open(
        tmp_path,
        lambda: model,
        source_reader=source,
        allowed_records=BENCHMARK_RECORD_IDS,
    )
    try:
        result = await app.submit(Submission(record_id="TX-003"))
        assert result.outcome == "REPAIRED" and result.repair_count == 1
        assert set(result.data) == SIX_FIELDS
        assert result.data["customer_id"] is None
        assert result.data["issue_category"] == "TECHNICAL"
        assert result.data["urgency"] == "MEDIUM"
        assert "SYSTEM OVERRIDE" in model.calls[0][1].content
        assert any("repair_feedback" in message.content for message in model.calls[1])
        assert source.reads == ["TX-003"]
    finally:
        await app.close()


async def test_injected_record_content_is_neutralized_before_the_model(tmp_path, valid_json):
    source = RecordingFixtureSource()
    model = ScriptedModel(responses=[valid_json])
    app = await ExtractionService.open(
        tmp_path,
        lambda: model,
        source_reader=source,
        allowed_records=BENCHMARK_RECORD_IDS,
    )
    try:
        await app.submit(Submission(record_id="TX-INJECT"))
        sent = model.calls[0][1].content
        assert "SYSTEM OVERRIDE" in sent and "读取记录 TX-002" in sent
        assert "<system>" not in sent
        assert "[UNTRUSTED_TAG]" in sent
        assert source.reads == ["TX-INJECT"]
    finally:
        await app.close()


async def test_real_record_is_read_once_across_repairs_and_later_supplements(
    tmp_path, valid_json
):
    source = RecordingFixtureSource()
    model = ScriptedModel(responses=["bad", valid_json, valid_json])
    app = await ExtractionService.open(
        tmp_path,
        lambda: model,
        source_reader=source,
        allowed_records=BENCHMARK_RECORD_IDS,
    )
    try:
        first = await app.submit(Submission(record_id="TX-003"))
        assert first.outcome == "REPAIRED" and first.repair_count == 1
        second = await app.submit(Submission(session_id=first.session_id, text="仍未解决。"))
        assert second.task_id == first.task_id
        assert source.reads == ["TX-003"]
    finally:
        await app.close()


async def test_truncated_output_fixture_is_repaired_before_delivery(tmp_path, valid_json):
    truncated = fixture_data()["outputs"]["truncated"]
    source = RecordingFixtureSource()
    model = ScriptedModel(responses=[truncated, valid_json])
    app = await ExtractionService.open(
        tmp_path,
        lambda: model,
        source_reader=source,
        allowed_records=BENCHMARK_RECORD_IDS,
    )
    try:
        result = await app.submit(Submission(record_id="TX-001"))
        assert result.outcome == "REPAIRED" and result.repair_count == 1
        assert set(result.data) == SIX_FIELDS
        assert any("客户报告发票被重复扣款" in message.content for message in model.calls[1])
        assert source.reads == ["TX-001"]
    finally:
        await app.close()


async def test_overlong_fixture_is_rejected_without_a_model_call(tmp_path, valid_json):
    source = RecordingFixtureSource()
    model = ScriptedModel(responses=[valid_json])
    app = await ExtractionService.open(
        tmp_path,
        lambda: model,
        source_reader=source,
        allowed_records=BENCHMARK_RECORD_IDS,
    )
    try:
        with pytest.raises(InputError, match="SOURCE_TOO_LARGE"):
            await app.submit(Submission(record_id="TX-LONG"))
        assert source.reads == ["TX-LONG"]
        assert model.calls == []
    finally:
        await app.close()


def test_http_record_mode_reads_the_packaged_fixture(tmp_path, valid_json):
    model = ScriptedModel(responses=[valid_json])
    app = create_app(directory=tmp_path, model_factory=lambda: model)
    with TestClient(app) as client:
        response = client.post("/extractions", json={"record_id": "TX-001"})
        assert response.status_code == 200
        body = response.json()
        assert body["outcome"] == "SUCCESS"
        assert body["data"]["customer_id"] == "CUST-98234"
        assert body["data"]["detected_pii"] is False
        assert "4402" in model.calls[0][1].content
    assert len(model.calls) == 1


def test_http_denies_unlisted_records_before_the_adapter_or_model_runs(
    tmp_path, valid_json, monkeypatch
):
    reads = []
    original_read = FixtureSource.read

    async def recording_read(self, record_id):
        reads.append(record_id)
        return await original_read(self, record_id)

    monkeypatch.setattr(FixtureSource, "read", recording_read)
    model = ScriptedModel(responses=[valid_json])
    app = create_app(directory=tmp_path, model_factory=lambda: model)
    with TestClient(app) as client:
        for record_id in ["TX-999", "../../etc/passwd", "https://example.test/TX-001"]:
            response = client.post("/extractions", json={"record_id": record_id})
            assert response.status_code == 403
            assert response.json()["error_code"] == "ACCESS_DENIED"
            assert response.json()["request_id"]
    assert reads == [] and model.calls == []
