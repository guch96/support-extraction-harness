from conftest import ScriptedModel

from entrypoints import ui
from harness.contracts import RunResult, ServiceError
from infrastructure.model import configured_model
from infrastructure.records import FixtureSource
from support_extraction.benchmark import BENCHMARK_RECORD_IDS, FIXTURE_PATH
from support_extraction.policy import SIX_FIELDS
from support_extraction.service import ExtractionService


async def open_service(tmp_path, responses, **options):
    model = ScriptedModel(responses=responses)
    service = await ExtractionService.open(tmp_path, lambda: model, **options)
    return service, model


async def test_direct_extraction_shows_all_six_fields_and_run_metrics(
    tmp_path, valid_json, valid_data
):
    service, model = await open_service(tmp_path, [valid_json])
    try:
        panel = await ui.extract(service, text="重复扣款，已发起退款。")
        assert panel["status"] == "SUCCESS"
        assert set(panel["fields"]) == set(SIX_FIELDS)
        assert panel["fields"] == valid_data
        assert panel["model_calls"] == 1
        assert panel["repair_count"] == 0
        assert panel["notice"] == ui.SUCCESS_NOTICE
        assert panel["session_id"] and panel["task_id"] and panel["request_id"]
        rendered = ui.format_panel(panel)
        assert "SUCCESS" in rendered
        assert "模型调用次数：1" in rendered and "修复次数：0" in rendered
        assert "CUST-98234" in rendered
        assert len(model.calls) == 1
    finally:
        await service.close()


async def test_repaired_record_run_and_supplement_reuse_the_current_task(
    tmp_path, valid_json
):
    source = FixtureSource(FIXTURE_PATH, BENCHMARK_RECORD_IDS)
    service, model = await open_service(
        tmp_path,
        ["bad", valid_json, valid_json],
        source_reader=source,
        allowed_records=BENCHMARK_RECORD_IDS,
    )
    try:
        first = await ui.extract(service, record_id="TX-001")
        assert first["status"] == "REPAIRED"
        assert first["repair_count"] == 1
        assert first["notice"] == ui.REPAIRED_NOTICE.format(repair_count=1)
        assert first["fields"]["customer_id"] == "CUST-98234"

        supplement = await ui.extract(
            service,
            text="退款仍未到账。",
            session_id=first["session_id"],
            task_id=first["task_id"],
        )
        assert supplement["status"] == "SUCCESS"
        assert supplement["session_id"] == first["session_id"]
        assert supplement["task_id"] == first["task_id"]
        restored = await service.restore(first["session_id"])
        assert [source["text"] for source in restored["sources"]][-1] == "退款仍未到账。"
        assert len(model.calls) == 3
    finally:
        await service.close()


async def test_restore_lists_saved_sources_and_historical_results(tmp_path, valid_json):
    service, model = await open_service(tmp_path, [valid_json, valid_json])
    try:
        first = await ui.extract(service, text="客户 ID CUST-98234，重复扣款。")
        second = await ui.extract(
            service, text="退款已发起。", session_id=first["session_id"]
        )
        panel = await ui.restore_session(service, first["session_id"])
        assert panel["kind"] == "session"
        assert panel["session_id"] == first["session_id"]
        assert panel["task_id"] == first["task_id"]
        assert [source["text"] for source in panel["sources"]] == [
            "客户 ID CUST-98234，重复扣款。",
            "退款已发起。",
        ]
        assert [run["request_id"] for run in panel["history"]] == [
            first["request_id"],
            second["request_id"],
        ]
        for run in panel["history"]:
            assert run["notice"] == (
                f"这是请求 {run['request_id']} 的历史结果，未必反映最新补充。"
            )
        rendered = ui.format_panel(panel)
        assert "**历史有效结果**" in rendered
        assert ui.HISTORICAL_NOTICE.format(request_id=first["request_id"]) in rendered
        assert "客户 ID CUST-98234，重复扣款。" in rendered
        assert len(model.calls) == 2
    finally:
        await service.close()


async def test_new_task_does_not_inherit_previous_customer_facts(tmp_path, valid_json):
    service, model = await open_service(tmp_path, [valid_json, valid_json])
    try:
        first = await ui.extract(service, text="客户 ID CUST-98234，重复扣款。")
        second = await ui.extract(
            service,
            text="一般咨询。",
            session_id=first["session_id"],
            new_task=True,
        )
        assert second["session_id"] == first["session_id"]
        assert second["task_id"] != first["task_id"]
        assert "CUST-98234" not in model.calls[-1][1].content
        restored = await service.restore(first["session_id"])
        assert [source["text"] for source in restored["sources"]] == ["一般咨询。"]
    finally:
        await service.close()


async def test_new_task_ignores_a_stale_task_id_from_the_page_state(tmp_path, valid_json):
    service, model = await open_service(tmp_path, [valid_json, valid_json])
    try:
        first = await ui.extract(service, text="客户 ID CUST-98234，重复扣款。")
        second = await ui.extract(
            service,
            text="一般咨询。",
            session_id=first["session_id"],
            task_id=first["task_id"],
            new_task=True,
        )
        assert second["status"] == "SUCCESS"
        assert second["session_id"] == first["session_id"]
        assert second["task_id"] != first["task_id"]
    finally:
        await service.close()


async def test_clear_makes_the_session_unavailable(tmp_path, valid_json):
    service, model = await open_service(tmp_path, [valid_json])
    try:
        result = await ui.extract(service, text="客户 ID CUST-98234，重复扣款。")
        cleared = await ui.clear_session(service, result["session_id"])
        assert cleared["kind"] == "cleared"
        assert "清空" in cleared["notice"]
        again = await ui.restore_session(service, result["session_id"])
        assert again["kind"] == "error"
        assert again["status"] == "REJECTED"
        assert again["error_code"] == "SESSION_EXPIRED"
        assert "已过期或已清空" in again["notice"]
        assert len(model.calls) == 1
    finally:
        await service.close()


async def test_reviews_hide_raw_until_requested_and_mark_expired_evidence(tmp_path):
    now = [1000000.0]
    service, model = await open_service(
        tmp_path, ["bad", "bad", "bad"], clock=lambda: now[0]
    )
    try:
        failed = await ui.extract(service, text="客户支付失败。")
        assert failed["status"] == "FALLBACK"
        assert failed["fields"] == {}
        assert failed["review_id"]
        assert failed["notice"] == ui.FALLBACK_NOTICE.format(
            review_id=failed["review_id"]
        )
        assert "审核编号" in ui.format_panel(failed)

        reviews = await ui.list_reviews(service)
        assert reviews["kind"] == "reviews"
        assert [row["review_id"] for row in reviews["reviews"]] == [
            failed["review_id"]
        ]
        assert failed["review_id"] in ui.format_panel(reviews)

        detail = await ui.review_detail(service, failed["review_id"])
        assert detail["raw_visible"] is False
        assert "raw_text" not in detail["sources"][0]
        assert "raw" not in detail["attempts"][0]
        assert detail["sources"][0]["text"] == "客户支付失败。"

        raw = await ui.review_detail(service, failed["review_id"], include_raw=True)
        assert raw["raw_visible"] is True
        assert raw["sources"][0]["raw_text"] == "客户支付失败。"
        assert raw["attempts"][0]["raw"] is not None
        assert len(raw["attempts"]) == 3

        now[0] += 8 * 86400
        expired = await ui.review_detail(service, failed["review_id"], include_raw=True)
        assert expired["expired"] is True
        assert expired["notice"] == ui.EXPIRED_NOTICE
        assert expired["sources"][0]["raw_status"] == "EXPIRED"
        assert expired["sources"][0]["raw_text"] is None
        rendered = ui.format_panel(expired)
        assert ui.EXPIRED_NOTICE in rendered
        assert "原文已过期" in rendered
        assert len(model.calls) == 3
    finally:
        await service.close()


def test_fallback_wording_requires_a_saved_review_id():
    saved = RunResult(
        session_id="session-1",
        task_id="task-1",
        request_id="request-1",
        outcome="FALLBACK",
        model_calls=3,
        repair_count=2,
        review_id="review-1",
        error_code="OUTPUT_INVALID",
    )
    panel = ui.result_panel(saved)
    assert panel["notice"] == ui.FALLBACK_NOTICE.format(review_id="review-1")
    assert "review-1" in ui.format_panel(panel)

    unsaved = saved.model_copy(update={"review_id": None})
    panel = ui.result_panel(unsaved)
    assert "待人工审核" not in panel["notice"]
    assert panel["notice"] == ui.REVIEW_SAVE_FAILED_NOTICE.format(
        request_id="request-1"
    )
    assert "审核编号" not in ui.format_panel(panel)


async def test_service_errors_report_the_source_saved_fact(tmp_path, valid_json):
    model = ScriptedModel(responses=[valid_json])
    factory_calls = []

    def factory():
        factory_calls.append(1)
        if len(factory_calls) > 1:
            raise ServiceError("MODEL_NOT_CONFIGURED", "")
        return model

    service = await ExtractionService.open(tmp_path, factory)
    try:
        first = await ui.extract(service, text="客户 ID CUST-98234，重复扣款。")
        assert first["status"] == "SUCCESS"
        failed = await ui.extract(
            service, text="修正：客户 ID 应为 CUST-456。", session_id=first["session_id"]
        )
        assert failed["status"] == "SERVICE_ERROR"
        assert failed["error_code"] == "MODEL_NOT_CONFIGURED"
        assert failed["notice"] == ui.SAVED_BUT_ERROR_NOTICE.format(
            request_id=failed["request_id"]
        )
        assert "MODEL_NOT_CONFIGURED" in ui.format_panel(failed)
        restored = await ui.restore_session(service, first["session_id"])
        assert [source["text"] for source in restored["sources"]][-1] == (
            "修正：客户 ID 应为 CUST-456。"
        )
    finally:
        await service.close()


async def test_source_save_failure_is_reported_without_calling_the_model(
    tmp_path, valid_json
):
    model = ScriptedModel(responses=[valid_json])
    service = await ExtractionService.open(tmp_path, lambda: model)

    def fail(*args, **kwargs):
        raise OSError("disk full")

    service.store.append_source = fail
    try:
        panel = await ui.extract(service, text="支付失败。")
        assert panel["status"] == "SERVICE_ERROR"
        assert panel["notice"] == ui.SOURCE_SAVE_FAILED_NOTICE.format(
            request_id=panel["request_id"]
        )
        assert model.calls == []
    finally:
        await service.close()


async def test_review_save_failure_never_claims_a_fallback_review(tmp_path):
    service, model = await open_service(tmp_path, ["bad", "bad", "bad"])
    service.store.run_finished = lambda result: None
    try:
        panel = await ui.extract(service, text="支付失败。")
        assert panel["status"] == "SERVICE_ERROR"
        assert panel["review_id"] is None
        assert panel["notice"] == ui.REVIEW_SAVE_FAILED_NOTICE.format(
            request_id=panel["request_id"]
        )
        assert "待人工审核。审核编号" not in ui.format_panel(panel)
    finally:
        await service.close()


async def test_missing_model_configuration_is_an_explicit_error_not_a_substitute(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    service = await ExtractionService.open(tmp_path, configured_model)
    try:
        panel = await ui.extract(service, text="支付失败。")
        assert panel["status"] == "SERVICE_ERROR"
        assert panel["error_code"] == "MODEL_NOT_CONFIGURED"
        assert "MODEL_NOT_CONFIGURED" in ui.format_panel(panel)
        assert panel["review_id"] is None
    finally:
        await service.close()
