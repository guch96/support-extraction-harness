import asyncio
import json

import pytest
from conftest import ScriptedModel

from support_extraction.service import ExtractionService, InputError, Submission


async def test_session_reopens_without_calling_model(tmp_path, valid_json):
    model = ScriptedModel(responses=[valid_json, valid_json])
    first = await ExtractionService.open(tmp_path, lambda: model)
    result = await first.submit(Submission(text="客户 ID CUST-98234，重复扣款。"))
    await first.close()
    second = await ExtractionService.open(tmp_path, lambda: model)
    restored = await second.restore(result.session_id)
    assert restored["current_task_id"] == result.task_id
    assert len(model.calls) == 1
    again = await second.submit(Submission(session_id=result.session_id, text="退款还没到账。"))
    assert again.model_calls == 1
    assert "重复扣款" in model.calls[-1][1].content
    assert "退款还没到账" in model.calls[-1][1].content
    assert len(model.calls[-1]) == 2
    await second.close()


async def test_saved_correction_survives_fallback_and_review_is_retrievable(
    tmp_path, valid_json, valid_data
):
    corrected = json.dumps({**valid_data, "customer_id": "CUST-456"})
    model = ScriptedModel(responses=[valid_json, "bad", "bad", "bad", corrected])
    app = await ExtractionService.open(tmp_path, lambda: model)
    try:
        original = await app.submit(Submission(text="客户 ID CUST-123，重复扣款。"))
        failed = await app.submit(
            Submission(session_id=original.session_id, text="修正：客户 ID 应为 CUST-456。")
        )
        assert failed.outcome == "FALLBACK" and failed.data is None
        review = await app.review(failed.review_id, raw=True)
        assert review["status"] == "PENDING"
        assert len(review["attempts"]) == 3
        assert review["sources"][-1]["raw_text"] == "修正：客户 ID 应为 CUST-456。"
        result = await app.submit(Submission(session_id=original.session_id, text="仍未解决。"))
        assert result.data["customer_id"] == "CUST-456"
        assert "CUST-456" in model.calls[-1][1].content
        assert len(model.calls[-1]) == 2
    finally:
        await app.close()


async def test_session_cannot_append_to_another_sessions_task(tmp_path, valid_json):
    model = ScriptedModel(responses=[valid_json] * 3)
    app = await ExtractionService.open(tmp_path, lambda: model)
    try:
        first = await app.submit(Submission(text="密码是 SuperSecretPass123。支付崩溃。"))
        second = await app.submit(Submission(text="一般咨询。"))
        assert second.data["detected_pii"] is False
        assert "支付崩溃" not in model.calls[-1][1].content
        with pytest.raises(InputError, match="TASK_NOT_FOUND"):
            await app.submit(
                Submission(session_id=second.session_id, task_id=first.task_id, text="越权补充")
            )
        restored = await app.restore(first.session_id)
        assert len(restored["sources"]) == 1
        assert len(model.calls) == 2
    finally:
        await app.close()


async def test_busy_task_rejects_concurrent_supplement_without_saving_it(tmp_path, valid_json):
    started, release = asyncio.Event(), asyncio.Event()

    async def slow():
        started.set()
        await release.wait()
        return valid_json

    model = ScriptedModel(responses=[valid_json, slow, valid_json])
    app = await ExtractionService.open(tmp_path, lambda: model)
    first = await app.submit(Submission(text="重复扣款。"))
    pending = asyncio.create_task(
        app.submit(Submission(session_id=first.session_id, text="补充一"))
    )
    await started.wait()
    try:
        with pytest.raises(InputError, match="TASK_BUSY"):
            await app.submit(Submission(session_id=first.session_id, text="不该保存的并发补充"))
        restored = await app.restore(first.session_id)
        assert len(restored["sources"]) == 2
        assert len(model.calls) == 2
    finally:
        release.set()
        await pending
        await app.close()
