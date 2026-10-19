import re

import pytest
from conftest import ScriptedModel

from entrypoints import chat
from infrastructure.model import configured_model
from infrastructure.records import FixtureSource
from runtime.contracts import RunResult, ServiceError
from support_extraction.benchmark import BENCHMARK_RECORD_IDS, FIXTURE_PATH
from support_extraction.policy import SIX_FIELDS
from support_extraction.service import ExtractionService, Submission


async def open_service(tmp_path, responses, **options):
    model = ScriptedModel(responses=responses)
    service = await ExtractionService.open(tmp_path, lambda: model, **options)
    return service, model


async def collect(states):
    return [state async for state in states]


def user_texts(messages):
    return [message["content"] for message in messages if message["role"] == "user"]


def assistant_texts(messages):
    return [
        message["content"] for message in messages if message["role"] == "assistant"
    ]


def model_text(messages):
    return "\n".join(str(message.content) for message in messages)


async def test_first_text_creates_a_session_and_renders_the_validated_card(
    tmp_path, valid_json
):
    service, model = await open_service(tmp_path, [valid_json])
    try:
        states = await collect(
            chat.send_text(service, "重复扣款，已发起退款。", None, None, False)
        )
        assert states[0]["messages"][-1]["content"] == chat.PLACEHOLDER_NOTICE
        state = states[-1]
        assert state["session_id"] and state["task_id"]
        assert "当前任务" in state["status"]
        assert state["task_id"] in state["status"]
        assert "来源 **1** 条" in state["status"]
        assert state["armed"] is False
        assert state["fallback_review_id"] is None

        assert user_texts(state["messages"]) == ["重复扣款，已发起退款。"]
        card = assistant_texts(state["messages"])[-1]
        assert "SUCCESS" in card
        answer = (
            "这份对话的核心问题是：重复扣款，退款已发起，预计 3-5 个工作日到账。\n\n"
            "归类为账单问题，紧急程度中。客户标识：CUST-98234。"
            "后续步骤：1. 等待已发起的退款到账。\n\n"
            "依据已校验字段整理。"
        )
        assert card.startswith(f"### 🟢 SUCCESS\n{answer}\n")
        for label in chat.FIELD_LABELS.values():
            assert label in card
        assert set(chat.FIELD_LABELS) == set(SIX_FIELDS)
        assert "CUST-98234" in card and "BILLING" in card
        assert "模型调用次数：1" in card and "修复次数：0" in card
        assert "<details>" in card and "结构化结果（已校验）" in card
        assert "```json" in card
        details_start = card.index("<details>")
        assert (
            card.index(answer)
            < details_start
            < card.index("| 字段 |")
            < card.index("```json")
            < card.index("</details>")
        )

        restored = await service.restore(state["session_id"])
        request_id = restored["runs"][0]["id"]
        assert f"`{request_id}`" in card
        assert len(model.calls) == 1
    finally:
        await service.close()


async def test_free_text_streams_each_phase_before_the_final_card(tmp_path, valid_json):
    service, model = await open_service(tmp_path, [valid_json])
    try:
        states = await collect(chat.send_text(service, "重复扣款。", None, None, False))
        streamed = [
            content
            for state in states
            for content in assistant_texts(state["messages"])
        ]
        assert any("正在调用模型" in content for content in streamed)
        assert any("正在校验输出" in content for content in streamed)
        assert any("提取完成" in content for content in streamed)
        assert "SUCCESS" in assistant_texts(states[-1]["messages"])[-1]
        assert len(model.calls) == 1
    finally:
        await service.close()


async def test_second_free_text_supplements_the_same_task(tmp_path, valid_json):
    service, model = await open_service(tmp_path, [valid_json, valid_json])
    try:
        first = (
            await collect(
                chat.send_text(service, "客户 ID CUST-98234，重复扣款。", None, None, False)
            )
        )[-1]
        second = (
            await collect(
                chat.send_text(
                    service,
                    "退款仍未到账。",
                    first["session_id"],
                    first["task_id"],
                    False,
                    messages=first["messages"],
                )
            )
        )[-1]
        assert second["session_id"] == first["session_id"]
        assert second["task_id"] == first["task_id"]
        assert second["armed"] is False
        assert "来源 **2** 条" in second["status"]
        assert user_texts(second["messages"]) == [
            "客户 ID CUST-98234，重复扣款。",
            "退款仍未到账。",
        ]
        sent = model_text(model.calls[-1])
        assert "客户 ID CUST-98234，重复扣款。" in sent
        assert "退款仍未到账。" in sent

        restored = await service.restore(first["session_id"])
        assert [source["text"] for source in restored["sources"]] == [
            "客户 ID CUST-98234，重复扣款。",
            "退款仍未到账。",
        ]
        assert len(model.calls) == 2
    finally:
        await service.close()


async def test_armed_new_task_consumes_the_arm_and_starts_a_new_task(
    tmp_path, valid_json
):
    service, model = await open_service(tmp_path, [valid_json, valid_json])
    try:
        first = (
            await collect(
                chat.send_text(service, "客户 ID CUST-98234，重复扣款。", None, None, False)
            )
        )[-1]
        assert chat.arm_new_task() is True
        second = (
            await collect(
                chat.send_text(
                    service,
                    "一般咨询。",
                    first["session_id"],
                    first["task_id"],
                    True,
                    messages=first["messages"],
                )
            )
        )[-1]
        assert second["session_id"] == first["session_id"]
        assert second["task_id"] != first["task_id"]
        assert second["armed"] is False
        assert "来源 **1** 条" in second["status"]
        sent = model_text(model.calls[-1])
        assert "一般咨询。" in sent
        assert "CUST-98234" not in sent

        restored = await service.restore(first["session_id"])
        assert [source["text"] for source in restored["sources"]] == ["一般咨询。"]
    finally:
        await service.close()


async def test_record_send_always_starts_a_new_task(tmp_path, valid_json):
    source = FixtureSource(FIXTURE_PATH, BENCHMARK_RECORD_IDS)
    service, model = await open_service(
        tmp_path,
        [valid_json, valid_json],
        source_reader=source,
        allowed_records=BENCHMARK_RECORD_IDS,
    )
    try:
        first = (
            await collect(
                chat.send_text(service, "客户 ID CUST-111，重复扣款。", None, None, False)
            )
        )[-1]
        second = (
            await collect(
                chat.send_record(
                    service, "TX-001", first["session_id"], messages=first["messages"]
                )
            )
        )[-1]
        assert second["session_id"] == first["session_id"]
        assert second["task_id"] != first["task_id"]
        assert "SUCCESS" in assistant_texts(second["messages"])[-1]
        assert user_texts(second["messages"])[-1] == "读取预置记录：`TX-001`"
        sent = model_text(model.calls[-1])
        assert "CUST-111" not in sent

        restored = await service.restore(first["session_id"])
        assert len(restored["sources"]) == 1
        assert restored["sources"][0]["text"] != "客户 ID CUST-111，重复扣款。"
        assert len(model.calls) == 2
    finally:
        await service.close()


async def test_restore_rebuilds_every_task_with_dividers_bubbles_and_cards(
    tmp_path, valid_json
):
    service, model = await open_service(tmp_path, [valid_json, valid_json, valid_json])
    try:
        first = (
            await collect(
                chat.send_text(service, "客户 ID CUST-98234，重复扣款。", None, None, False)
            )
        )[-1]
        supplement = (
            await collect(
                chat.send_text(
                    service,
                    "退款已发起。",
                    first["session_id"],
                    first["task_id"],
                    False,
                    messages=first["messages"],
                )
            )
        )[-1]
        second_task = (
            await collect(
                chat.send_text(
                    service,
                    "一般咨询。",
                    supplement["session_id"],
                    supplement["task_id"],
                    True,
                    messages=supplement["messages"],
                )
            )
        )[-1]

        state = await chat.restore(service, first["session_id"])
        assert state["session_id"] == first["session_id"]
        assert state["task_id"] == second_task["task_id"]
        assert state["task_id"] in state["status"]
        assert "来源 **1** 条" in state["status"]

        assistants = assistant_texts(state["messages"])
        dividers = [text for text in assistants if text.startswith("---")]
        cards = [text for text in assistants if "SUCCESS" in text]
        assert len(dividers) == 2
        assert first["task_id"] in dividers[0]
        assert second_task["task_id"] in dividers[1]
        assert len(cards) == 3
        assert user_texts(state["messages"]) == [
            "客户 ID CUST-98234，重复扣款。",
            "退款已发起。",
            "一般咨询。",
        ]
        history = await service.history(first["session_id"])
        request_ids = [
            run["request_id"] for task in history for run in task["runs"]
        ]
        assert len(request_ids) == 3
        joined = "\n".join(cards)
        for request_id in request_ids:
            assert chat.HISTORICAL_NOTICE.format(request_id=request_id) in joined
        assert len(model.calls) == 3
    finally:
        await service.close()


async def test_clear_and_new_session_reset_the_page_and_the_session_is_gone(
    tmp_path, valid_json
):
    service, model = await open_service(tmp_path, [valid_json])
    try:
        sent = (
            await collect(chat.send_text(service, "重复扣款。", None, None, False))
        )[-1]
        cleared = await chat.clear(service, sent["session_id"])
        assert cleared["messages"] == []
        assert cleared["session_id"] == ""
        assert cleared["task_id"] is None
        assert cleared["armed"] is False
        assert cleared["fallback_review_id"] is None
        assert "尚未开始" in cleared["status"]

        again = await chat.restore(service, sent["session_id"])
        assert "SESSION_EXPIRED" in assistant_texts(again["messages"])[-1]

        assert await chat.clear(service, None) == chat.new_session()
        fresh = chat.new_session()
        assert fresh["messages"] == [] and fresh["session_id"] == ""
        assert fresh["task_id"] is None and "尚未开始" in fresh["status"]
        assert len(model.calls) == 1
    finally:
        await service.close()


async def test_fallback_shows_the_review_action_bar_without_leaking_raw_output(
    tmp_path,
):
    secret = "SuperSecretPass123"
    service, model = await open_service(
        tmp_path, [f"{secret} refund called", "raw provider failure", "another raw failure"]
    )
    try:
        states = await collect(
            chat.send_text(service, f"密码是 {secret}，支付崩溃。", None, None, False)
        )
        state = states[-1]
        assert state["fallback_review_id"]
        assert "本次 FALLBACK" in state["fallback_notice"]
        assert state["fallback_review_id"] in state["fallback_notice"]
        card = assistant_texts(state["messages"])[-1]
        assert "FALLBACK" in card
        assert state["fallback_review_id"] in card
        assert "这次没有取得合格结果，已保存为待人工审核，编号" in card
        assert "原因：模型输出未通过检查。" in card
        assert "| 字段 |" not in card
        assert "结构化结果（已校验）" not in card
        assert "```json" not in card
        assert "模型调用次数：3" in card and "修复次数：2" in card

        everything = "\n".join(assistant_texts(state["messages"]))
        for item in states:
            everything += "\n" + "\n".join(assistant_texts(item["messages"]))
        for raw in (secret, "refund called", "raw provider failure", "another raw failure"):
            assert raw not in everything

        action = chat.open_review(state["fallback_review_id"])
        assert action == {
            "tab": chat.REVIEW_TAB_ID,
            "review_id": state["fallback_review_id"],
        }
        assert chat.open_review(None) == {"tab": chat.REVIEW_TAB_ID, "review_id": ""}
        assert len(model.calls) == 3
    finally:
        await service.close()


async def test_reviews_hide_raw_until_requested_and_mark_expired_evidence(tmp_path):
    now = [1000000.0]
    service, model = await open_service(
        tmp_path, ["bad", "bad", "bad"], clock=lambda: now[0]
    )
    try:
        failed = (
            await collect(chat.send_text(service, "客户支付失败。", None, None, False))
        )[-1]
        reviews = await chat.list_reviews(service)
        assert reviews["kind"] == "reviews"
        assert [row["review_id"] for row in reviews["reviews"]] == [
            failed["fallback_review_id"]
        ]
        assert failed["fallback_review_id"] in chat.format_panel(reviews)

        detail = await chat.review_detail(service, failed["fallback_review_id"])
        assert detail["raw_visible"] is False
        assert "raw_text" not in detail["sources"][0]
        assert "raw" not in detail["attempts"][0]
        assert detail["sources"][0]["text"] == "客户支付失败。"
        assert len(detail["attempts"]) == 3
        assert all(
            attempt["validation_stage"] == "raw_schema"
            and attempt["error_signature"]
            for attempt in detail["attempts"]
        )
        rendered = chat.format_panel(detail)
        assert "raw_schema" in rendered
        assert "耗时 " in rendered and " ms" in rendered
        assert "原文：" not in rendered
        assert '"bad"' not in rendered

        raw = await chat.review_detail(
            service, failed["fallback_review_id"], include_raw=True
        )
        assert raw["raw_visible"] is True
        assert raw["sources"][0]["raw_text"] == "客户支付失败。"
        assert raw["attempts"][0]["raw"] is not None
        assert len(raw["attempts"]) == 3
        raw_rendered = chat.format_panel(raw)
        assert "原文：" in raw_rendered
        assert '"bad"' in raw_rendered

        now[0] += 8 * 86400
        expired = await chat.review_detail(
            service, failed["fallback_review_id"], include_raw=True
        )
        assert expired["expired"] is True
        assert expired["notice"] == chat.EXPIRED_NOTICE
        assert expired["sources"][0]["raw_status"] == "EXPIRED"
        assert expired["sources"][0]["raw_text"] is None
        rendered = chat.format_panel(expired)
        assert chat.EXPIRED_NOTICE in rendered
        assert "原文已过期" in rendered
        assert "raw_schema" in rendered and "耗时 " in rendered
        assert "原文：" not in rendered
        assert len(model.calls) == 3
    finally:
        await service.close()


async def test_missing_model_configuration_renders_an_explicit_error_card(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    service = await ExtractionService.open(tmp_path, configured_model)
    try:
        states = await collect(chat.send_text(service, "支付失败。", None, None, False))
        card = assistant_texts(states[-1]["messages"])[-1]
        assert "ERROR" in card
        assert "MODEL_NOT_CONFIGURED" in card
        assert "这次请求没有完成：模型未配置（`MODEL_NOT_CONFIGURED`）。" in card
        assert "请求编号：`" in card
        assert states[-1]["fallback_review_id"] is None
    finally:
        await service.close()


async def test_blank_text_renders_an_input_error_card(tmp_path, valid_json):
    service, model = await open_service(tmp_path, [valid_json])
    try:
        states = await collect(chat.send_text(service, "   ", None, None, False))
        card = assistant_texts(states[-1]["messages"])[-1]
        assert "ERROR" in card
        assert "INVALID_INPUT" in card
        assert "这次请求没有完成：输入不合法（`INVALID_INPUT`）。" in card
        assert "请提供对话文本" in card
        assert "请求编号：`" in card
        assert model.calls == []
    finally:
        await service.close()


async def test_message_too_large_renders_its_own_input_notice(tmp_path, valid_json):
    service, model = await open_service(tmp_path, [valid_json])
    try:
        states = await collect(
            chat.send_text(service, "x" * 1001, None, None, False)
        )
        card = assistant_texts(states[-1]["messages"])[-1]
        assert "ERROR" in card and "MESSAGE_TOO_LARGE" in card
        assert "这次请求没有完成：单条消息超过长度上限（`MESSAGE_TOO_LARGE`）。" in card
        assert chat.INPUT_NOTICES["MESSAGE_TOO_LARGE"] in card
        assert "来源材料超过上限" not in card
        assert model.calls == []
    finally:
        await service.close()


async def test_record_send_repairs_and_the_next_text_supplements_it(tmp_path, valid_json):
    source = FixtureSource(FIXTURE_PATH, BENCHMARK_RECORD_IDS)
    service, model = await open_service(
        tmp_path,
        ["bad", valid_json, valid_json],
        source_reader=source,
        allowed_records=BENCHMARK_RECORD_IDS,
    )
    try:
        first = (
            await collect(chat.send_record(service, "TX-001"))
        )[-1]
        card = assistant_texts(first["messages"])[-1]
        assert "REPAIRED" in card
        assert "修复次数：1" in card
        assert "经过 1 次自动纠正后通过检查。" in card
        assert "修复轨迹：" in card and "raw_schema" in card
        assert "依据已校验字段整理。" in card
        assert "CUST-98234" in card

        supplement = (
            await collect(
                chat.send_text(
                    service,
                    "退款仍未到账。",
                    first["session_id"],
                    first["task_id"],
                    False,
                    messages=first["messages"],
                )
            )
        )[-1]
        assert supplement["session_id"] == first["session_id"]
        assert supplement["task_id"] == first["task_id"]
        assert "来源 **2** 条" in supplement["status"]
        restored = await service.restore(first["session_id"])
        assert [source["text"] for source in restored["sources"]][-1] == "退款仍未到账。"
        assert len(model.calls) == 3
    finally:
        await service.close()


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
        first = (
            await collect(
                chat.send_text(service, "客户 ID CUST-98234，重复扣款。", None, None, False)
            )
        )[-1]
        failed = (
            await collect(
                chat.send_text(
                    service,
                    "修正：客户 ID 应为 CUST-456。",
                    first["session_id"],
                    first["task_id"],
                    False,
                    messages=first["messages"],
                )
            )
        )[-1]
        card = assistant_texts(failed["messages"])[-1]
        assert "ERROR" in card
        assert "MODEL_NOT_CONFIGURED" in card
        request_id = re.search(r"请求编号：`([^`]+)`", card).group(1)
        assert chat.SAVED_BUT_ERROR_NOTICE.format(request_id=request_id) in card
        assert "补充已保存" in card

        restored = await service.restore(first["session_id"])
        assert [source["text"] for source in restored["sources"]][-1] == (
            "修正：客户 ID 应为 CUST-456。"
        )
    finally:
        await service.close()


async def test_source_save_failure_is_reported_without_calling_the_model(
    tmp_path, valid_json
):
    service, model = await open_service(tmp_path, [valid_json])

    def fail(*args, **kwargs):
        raise OSError("disk full")

    service.store.append_source = fail
    try:
        states = await collect(chat.send_text(service, "支付失败。", None, None, False))
        card = assistant_texts(states[-1]["messages"])[-1]
        assert "ERROR" in card
        assert "本次补充未保存" in card
        assert model.calls == []
    finally:
        await service.close()


async def test_review_save_failure_never_claims_a_fallback_review(tmp_path):
    service, model = await open_service(tmp_path, ["bad", "bad", "bad"])
    service.store.run_finished = lambda result: None
    try:
        states = await collect(chat.send_text(service, "支付失败。", None, None, False))
        state = states[-1]
        card = assistant_texts(state["messages"])[-1]
        assert "ERROR" in card
        request_id = re.search(r"请求编号：`([^`]+)`", card).group(1)
        assert chat.REVIEW_SAVE_FAILED_NOTICE.format(request_id=request_id) in card
        assert "待人工审核。审核编号" not in card
        assert state["fallback_review_id"] is None
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
    panel = chat.result_panel(saved)
    assert panel["notice"] == chat.FALLBACK_NOTICE.format(review_id="review-1")
    assert "review-1" in chat.format_result_card(panel)

    unsaved = saved.model_copy(update={"review_id": None})
    panel = chat.result_panel(unsaved)
    assert "待人工审核" not in panel["notice"]
    assert panel["notice"] == chat.REVIEW_SAVE_FAILED_NOTICE.format(
        request_id="request-1"
    )
    assert "审核编号" not in chat.format_result_card(panel)


async def test_each_send_invokes_submit_exactly_once(tmp_path, valid_json):
    service, model = await open_service(tmp_path, [valid_json, valid_json])
    calls = []
    original = service.submit

    async def counted(submission, phases=None):
        calls.append(submission)
        return await original(submission, phases=phases)

    service.submit = counted
    try:
        first = (
            await collect(chat.send_text(service, "重复扣款。", None, None, False))
        )[-1]
        assert len(calls) == 1
        await collect(
            chat.send_text(
                service,
                "退款已发起。",
                first["session_id"],
                first["task_id"],
                False,
                messages=first["messages"],
            )
        )
        assert len(calls) == 2
        assert len(model.calls) == 2
    finally:
        await service.close()


@pytest.mark.parametrize(
    ("error_code", "reason"),
    [
        ("MODEL_TIMEOUT", "模型调用超时"),
        ("MODEL_CALL_FAILED", "模型调用失败"),
        ("OUTPUT_INVALID", "模型输出未通过检查"),
        ("BUDGET_EXHAUSTED", "修复次数用尽"),
        ("RUN_TIMEOUT", "整次运行超时"),
        ("RECORD_NOT_FOUND", "RECORD_NOT_FOUND"),
    ],
)
def test_fallback_answer_maps_reasons_and_keeps_unknown_codes(error_code, reason):
    panel = chat.result_panel(
        RunResult(
            session_id="session-1",
            task_id="task-1",
            request_id="request-1",
            outcome="FALLBACK",
            model_calls=3,
            repair_count=2,
            review_id="review-1",
            error_code=error_code,
        )
    )
    assert chat.natural_answer(panel) == (
        "这次没有取得合格结果，已保存为待人工审核，编号 `review-1`。"
        f"原因：{reason}。"
    )


def test_natural_answer_states_missing_facts_without_guessing(valid_data):
    fields = {
        **valid_data,
        "summary": "",
        "customer_id": None,
        "actionable_steps": [],
    }
    panel = chat.result_panel(
        RunResult(
            session_id="session-1",
            task_id="task-1",
            request_id="request-1",
            outcome="SUCCESS",
            data=fields,
            model_calls=1,
            repair_count=0,
            diagnostics={"pii": {"detected": False}},
        )
    )
    answer = chat.natural_answer(panel)
    assert answer == (
        "这份对话的核心问题是：（无摘要）\n\n"
        "归类为账单问题，紧急程度中。未提供客户标识。"
        "来源中没有明确的后续步骤。\n\n"
        "依据已校验字段整理。"
    )
    assert "CUST-98234" not in answer
    assert "材料中发现敏感信息" not in answer


def test_natural_answer_adds_the_pii_sentence_only_when_detected(valid_data):
    panel = chat.result_panel(
        RunResult(
            session_id="session-1",
            task_id="task-1",
            request_id="request-1",
            outcome="SUCCESS",
            data=valid_data,
            model_calls=1,
            repair_count=0,
            diagnostics={"pii": {"detected": False}},
        )
    )
    assert "材料中发现敏感信息" not in chat.natural_answer(panel)
    panel["diagnostics"] = {"pii": {"detected": True}}
    assert "材料中发现敏感信息，已脱敏后处理。" in chat.natural_answer(panel)


async def test_history_replay_uses_the_same_natural_answer(tmp_path, valid_json):
    service, _ = await open_service(tmp_path, [valid_json])
    try:
        result = await service.submit(Submission(text="重复扣款，已发起退款。"))
        live = chat.format_result_card(chat.result_panel(result))
        state = await chat.restore(service, result.session_id)
    finally:
        await service.close()
    replayed = assistant_texts(state["messages"])[-1]
    answer = chat.natural_answer(chat.result_panel(result))
    assert live.startswith(f"### 🟢 SUCCESS\n{answer}")
    assert answer in replayed
    assert chat.HISTORICAL_NOTICE.format(request_id=result.request_id) in replayed


def test_chat_module_imports_gradio_lazily():
    assert "gradio" not in vars(chat)
