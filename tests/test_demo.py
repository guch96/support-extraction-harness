from conftest import ScriptedModel

from entrypoints import chat
from infrastructure import model as model_module
from support_extraction.demo import DEMO_TEXT
from support_extraction.safety import scan
from support_extraction.service import ExtractionService, Submission


async def open_service(tmp_path, responses):
    model = ScriptedModel(responses=responses)
    service = await ExtractionService.open(tmp_path, lambda: model)
    return service, model


async def collect(states):
    return [state async for state in states]


def user_texts(messages):
    return [message["content"] for message in messages if message["role"] == "user"]


def assistant_texts(messages):
    return [
        message["content"] for message in messages if message["role"] == "assistant"
    ]


async def test_seed_review_demo_stores_a_pending_review_with_three_attempts(tmp_path):
    service, model = await open_service(tmp_path, [])
    try:
        result = await service.seed_review_demo()

        assert result.outcome == "FALLBACK"
        assert result.error_code == "OUTPUT_INVALID"
        assert result.model_calls == 3
        assert result.repair_count == 2
        assert result.review_id
        assert len(result.diagnostics["repairs"]) == 3
        assert result.diagnostics["input_mode"] == "text"
        assert result.diagnostics["pii"] == {
            "detected": False,
            "kinds": [],
            "redactions": 0,
        }
        assert scan(DEMO_TEXT)["kinds"] == []
        assert model.calls == []

        review = await service.review(result.review_id, raw=True)
        assert review["status"] == "PENDING"
        assert len(review["attempts"]) == 3
        assert review["sources"][0]["raw_text"] == DEMO_TEXT
        assert review["sources"][0]["safe_text"] == DEMO_TEXT
        assert review["diagnostics"]["repairs"] == result.diagnostics["repairs"]
        for attempt in review["attempts"]:
            assert attempt["raw"]
            assert attempt["feedback"]["error_signature"]
    finally:
        await service.close()


async def test_seed_review_demo_never_calls_the_configured_model(
    tmp_path, monkeypatch
):
    def explode():
        raise AssertionError("configured model factory must not be called")

    monkeypatch.setattr(model_module, "configured_model", explode)
    service = await ExtractionService.open(tmp_path, model_module.configured_model)
    try:
        result = await service.seed_review_demo()
        assert result.outcome == "FALLBACK"
        assert result.review_id
    finally:
        await service.close()


async def test_normal_submit_still_uses_the_configured_model_after_a_demo(
    tmp_path, valid_json
):
    service, model = await open_service(tmp_path, [valid_json])
    try:
        demo = await service.seed_review_demo()
        result = await service.submit(Submission(text="重复扣款，已发起退款。"))

        assert demo.outcome == "FALLBACK"
        assert result.outcome == "SUCCESS"
        assert result.session_id != demo.session_id
        assert result.diagnostics["input_mode"] == "text"
        assert len(model.calls) == 1
    finally:
        await service.close()


async def test_demo_review_handler_renders_the_fallback_card_and_review_action(
    tmp_path,
):
    service, model = await open_service(tmp_path, [])
    try:
        states = await collect(chat.demo_review(service, None))
        state = states[-1]

        assert state["fallback_review_id"]
        assert chat.DEMO_MESSAGE in assistant_texts(state["messages"])
        card = assistant_texts(state["messages"])[-1]
        assert "FALLBACK" in card
        assert "模型调用次数：3" in card and "修复次数：2" in card
        assert state["fallback_review_id"] in card
        assert state["fallback_notice"] == chat.FALLBACK_ACTION_NOTICE.format(
            review_id=state["fallback_review_id"]
        )
        assert user_texts(state["messages"]) == [DEMO_TEXT]
        assert state["session_id"] and state["task_id"]
        assert "来源 **1** 条" in state["status"]
        assert model.calls == []

        reviews = await chat.list_reviews(service)
        assert [row["review_id"] for row in reviews["reviews"]] == [
            state["fallback_review_id"]
        ]
        assert state["fallback_review_id"] in chat.format_panel(reviews)

        safe = await chat.review_detail(service, state["fallback_review_id"])
        assert all(
            "issue_category:literal_error" in attempt["error_signature"]
            for attempt in safe["attempts"]
        )
        rendered = chat.format_panel(safe)
        assert rendered.count("raw_schema") == 3
        assert "耗时 " in rendered and " ms" in rendered
        assert "原文：" not in rendered
        assert "TECHICAL" not in rendered

        detail = await chat.review_detail(
            service, state["fallback_review_id"], include_raw=True
        )
        assert len(detail["attempts"]) == 3
        assert detail["sources"][0]["text"] == DEMO_TEXT
        assert detail["sources"][0]["raw_text"] == DEMO_TEXT
        assert detail["raw_visible"] is True

        assert chat.open_review(state["fallback_review_id"]) == {
            "tab": chat.REVIEW_TAB_ID,
            "review_id": state["fallback_review_id"],
        }
    finally:
        await service.close()


async def test_demo_review_appends_to_history_and_supplements_the_current_task(
    tmp_path, valid_json
):
    service, model = await open_service(tmp_path, [valid_json])
    try:
        first = (
            await collect(
                chat.send_text(service, "客户 ID CUST-98234，重复扣款。", None, None, False)
            )
        )[-1]
        state = (
            await collect(
                chat.demo_review(
                    service, first["session_id"], messages=first["messages"]
                )
            )
        )[-1]

        assert state["session_id"] == first["session_id"]
        assert state["task_id"] == first["task_id"]
        assert "来源 **2** 条" in state["status"]
        assert user_texts(state["messages"]) == [
            "客户 ID CUST-98234，重复扣款。",
            DEMO_TEXT,
        ]
        assert assistant_texts(state["messages"])[-2] == chat.DEMO_MESSAGE
        assert "FALLBACK" in assistant_texts(state["messages"])[-1]
        assert len(model.calls) == 1
    finally:
        await service.close()
