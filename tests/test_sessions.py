import asyncio
import json
import sys
from pathlib import Path

import pytest
from conftest import ScriptedModel

from harness.contracts import ServiceError
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


async def test_idle_session_expires_across_restart_without_model_replay(tmp_path, valid_json):
    now = [1000000.0]
    model = ScriptedModel(responses=[valid_json])
    app = await ExtractionService.open(tmp_path, lambda: model, clock=lambda: now[0])
    result = await app.submit(Submission(text="普通咨询。"))
    await app.close()
    now[0] += 8 * 86400
    app = await ExtractionService.open(tmp_path, lambda: model, clock=lambda: now[0])
    try:
        with pytest.raises(InputError, match="SESSION_EXPIRED"):
            await app.restore(result.session_id)
        assert len(model.calls) == 1
    finally:
        await app.close()


async def test_cleared_session_cannot_restore_or_accept_more_sources(tmp_path, valid_json):
    model = ScriptedModel(responses=[valid_json])
    app = await ExtractionService.open(tmp_path, lambda: model)
    try:
        result = await app.submit(Submission(text="客户 ID CUST-123，一般咨询。"))
        await app.clear(result.session_id)
        with pytest.raises(InputError, match="SESSION_EXPIRED"):
            await app.restore(result.session_id)
        with pytest.raises(InputError, match="SESSION_EXPIRED"):
            await app.submit(Submission(session_id=result.session_id, text="新补充"))
        assert len(model.calls) == 1
    finally:
        await app.close()


async def test_active_session_keeps_redacted_sources_when_raw_evidence_expires(
    tmp_path, valid_json
):
    now = [1000000.0]
    model = ScriptedModel(responses=["密码是 SuperSecretPass123", "bad", "bad", valid_json])
    app = await ExtractionService.open(tmp_path, lambda: model, clock=lambda: now[0])
    try:
        failed = await app.submit(Submission(text="密码是 SuperSecretPass123。支付崩溃。"))
        assert (
            "SuperSecretPass123"
            in (await app.review(failed.review_id, raw=True))["sources"][0]["raw_text"]
        )
        now[0] += 6 * 86400
        await app.restore(failed.session_id)
        now[0] += 2 * 86400
        review = await app.review(failed.review_id, raw=True)
        assert review["sources"][0]["raw_text"] is None
        assert review["sources"][0]["raw_status"] == "EXPIRED"
        assert review["attempts"][0]["raw"] is None
        continued = await app.submit(Submission(session_id=failed.session_id, text="仍然崩溃。"))
        assert continued.data["detected_pii"] is True
        assert "支付崩溃" in model.calls[-1][1].content
        assert "SuperSecretPass123" not in str(model.calls)
    finally:
        await app.close()


async def test_crashed_run_is_reviewed_on_startup_without_replaying_model(tmp_path, valid_json):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).with_name("crash_worker.py")),
        str(tmp_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, errors = await process.communicate()
    assert process.returncode == 17, errors.decode()
    model = ScriptedModel(responses=[valid_json])
    app = await ExtractionService.open(tmp_path, lambda: model)
    try:
        reviews = await app.reviews()
        assert len(reviews) == 1
        review = await app.review(reviews[0]["review_id"])
        assert review["error_code"] == "INTERRUPTED"
        assert review["model_calls"] == 1 and review["repair_count"] == 0
        assert model.calls == []
        restored = await app.restore(review["session_id"])
        assert restored["sources"][0]["text"] == "崩溃前已保存的支持对话。"
    finally:
        await app.close()


async def test_oversized_supplement_is_rejected_without_appending(tmp_path, valid_json):
    model = ScriptedModel(responses=[valid_json])
    app = await ExtractionService.open(tmp_path, lambda: model, max_source_chars=20)
    try:
        result = await app.submit(Submission(text="客户重复扣款。"))
        with pytest.raises(InputError, match="SOURCE_TOO_LARGE"):
            await app.submit(Submission(session_id=result.session_id, text="很" * 20))
        restored = await app.restore(result.session_id)
        assert [s["text"] for s in restored["sources"]] == ["客户重复扣款。"]
        assert len(model.calls) == 1
    finally:
        await app.close()


@pytest.mark.parametrize(
    "submission",
    [
        Submission(),
        Submission(text="  "),
        Submission(text="支付失败", record_id="TX-001"),
        Submission(text="支付失败", task_id="orphan"),
        Submission(text="支付失败", new_task=True, task_id="ambiguous"),
    ],
)
async def test_invalid_submission_never_reaches_model(tmp_path, valid_json, submission):
    model = ScriptedModel(responses=[valid_json])
    app = await ExtractionService.open(tmp_path, lambda: model)
    try:
        with pytest.raises(InputError, match="INVALID_INPUT"):
            await app.submit(submission)
        assert model.calls == []
    finally:
        await app.close()


async def test_internal_failure_preserves_source_and_releases_task_for_next_submission(
    tmp_path, valid_json
):
    model = ScriptedModel(
        responses=[valid_json, RuntimeError("private provider failure"), valid_json]
    )
    app = await ExtractionService.open(tmp_path, lambda: model)
    try:
        first = await app.submit(Submission(text="初次对话。"))
        with pytest.raises(ServiceError) as caught:
            await app.submit(Submission(session_id=first.session_id, text="必须保留的修正。"))
        assert caught.value.source_saved is True
        assert "private provider failure" not in str(caught.value)
        next_result = await app.submit(Submission(session_id=first.session_id, text="继续处理。"))
        assert next_result.outcome == "SUCCESS"
        assert "必须保留的修正" in model.calls[-1][1].content
        assert await app.reviews() == []
    finally:
        await app.close()
